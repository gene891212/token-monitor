#!/usr/bin/env python3
"""Token Monitor — Claude / Codex 訂閱額度監控儀表板。

讀取本機既有憑證(Claude Code 的 Keychain 條目、Codex 的 ~/.codex/auth.json),
定期查詢兩邊官方的用量端點,提供手機可看的網頁儀表板。
Token 只存在記憶體與原本的憑證儲存位置,不寫入日誌、不經由 HTTP 對外暴露。
"""

import base64
import json
import os
import platform
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("TOKEN_MONITOR_PORT", "8787"))
TLS_PORT = int(os.environ.get("TOKEN_MONITOR_TLS_PORT", "8788"))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "history.db")
STATIC_DIR = os.path.join(BASE, "static")
CERT_DIR = os.path.join(BASE, "certs")
OS_NAME = platform.system()  # "Darwin" / "Windows" / "Linux"

POLL_SECONDS = 300          # 背景輪詢間隔(研究顯示 180s 以上安全)
MIN_FETCH_INTERVAL = 60     # 手動刷新最短間隔

CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
# Windows 優先讀 ~/.claude/.credentials.json,並相容舊版 Credential Manager target;
# 若都失敗,用 `set TOKEN_MONITOR_CLAUDE_CRED_TARGET=實際名稱` 覆寫
# (可在「認證管理員」→ Windows 認證 裡搜尋 claude 找到正確名稱)。
CLAUDE_WIN_CRED_TARGETS = [os.environ.get("TOKEN_MONITOR_CLAUDE_CRED_TARGET")] if os.environ.get(
    "TOKEN_MONITOR_CLAUDE_CRED_TARGET") else [
    "Claude Code-credentials",
    "Claude Code",
]
CLAUDE_FILE_CREDS_PATH = os.path.expanduser("~/.claude/.credentials.json")
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_UA = "claude-code/2.0.14"
CLAUDE_FALLBACK_CREDS = os.path.join(DATA_DIR, "claude_oauth.json")

CODEX_AUTH_PATH = os.path.expanduser("~/.codex/auth.json")
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_UA = "codex_cli_rs"


def http_json(method, url, headers=None, body=None, form=None, timeout=20):
    if form is not None:
        from urllib.parse import urlencode
        data = urlencode(form).encode()
    else:
        data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if form is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:
            payload = {"error": "http_error"}
        return e.code, payload
    except Exception as e:
        return 0, {"error": str(e)}


# ---------------------------------------------------------------- Claude ----
# 三個平台的憑證存放位置不同:
#   macOS   → Keychain(`security` 指令)
#   Windows → ~/.claude/.credentials.json,舊版回退 Credential Manager
#   Linux   → ~/.claude/.credentials.json(明碼檔案,官方本來就這樣存)

def _mac_read_keychain():
    out = subprocess.run(
        ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True, timeout=10,
    )
    if out.returncode == 0:
        return json.loads(out.stdout.strip())
    return None


def _mac_write_keychain(blob):
    out = subprocess.run(
        ["security", "add-generic-password", "-U",
         "-a", os.environ.get("USER", os.environ.get("LOGNAME", "user")),
         "-s", CLAUDE_KEYCHAIN_SERVICE, "-w", blob],
        capture_output=True, text=True, timeout=10,
    )
    return out.returncode == 0


def _win_credread():
    """透過 ctypes 呼叫 CredReadW,依序嘗試候選 target name。"""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.windll.advapi32

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    CRED_TYPE_GENERIC = 1
    for target in CLAUDE_WIN_CRED_TARGETS:
        p = ctypes.POINTER(CREDENTIAL)()
        ok = advapi32.CredReadW(ctypes.c_wchar_p(target), CRED_TYPE_GENERIC, 0, ctypes.byref(p))
        if not ok:
            continue
        try:
            cred = p.contents
            blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return target, blob.decode("utf-8")
        finally:
            advapi32.CredFree(p)
    return None, None


def _win_credwrite(target, blob_str):
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.windll.advapi32

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    blob_bytes = blob_str.encode("utf-8")
    buf = ctypes.create_string_buffer(blob_bytes, len(blob_bytes))
    cred = CREDENTIAL(
        Flags=0, Type=CRED_TYPE_GENERIC, TargetName=target, Comment=None,
        CredentialBlobSize=len(blob_bytes),
        CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)),
        Persist=CRED_PERSIST_LOCAL_MACHINE, AttributeCount=0, Attributes=None,
        TargetAlias=None, UserName=None,
    )
    return bool(advapi32.CredWriteW(ctypes.byref(cred), 0))


def claude_read_creds():
    """依平台讀取 Claude Code 憑證;若本地備援檔比較新則優先用備援檔。"""
    creds = None
    try:
        if OS_NAME == "Darwin":
            creds = _mac_read_keychain()
        elif OS_NAME == "Windows":
            if os.path.exists(CLAUDE_FILE_CREDS_PATH):
                with open(CLAUDE_FILE_CREDS_PATH, encoding="utf-8") as f:
                    creds = json.load(f)
            else:
                _, blob = _win_credread()
                if blob:
                    creds = json.loads(blob)
        else:  # Linux
            if os.path.exists(CLAUDE_FILE_CREDS_PATH):
                with open(CLAUDE_FILE_CREDS_PATH, encoding="utf-8") as f:
                    creds = json.load(f)
    except Exception:
        pass
    if os.path.exists(CLAUDE_FALLBACK_CREDS):
        try:
            with open(CLAUDE_FALLBACK_CREDS) as f:
                fallback = json.load(f)
            if not creds or fallback["claudeAiOauth"]["expiresAt"] > creds["claudeAiOauth"]["expiresAt"]:
                creds = fallback
        except Exception:
            pass
    return creds


def claude_write_creds(creds):
    blob = json.dumps(creds)
    wrote_back = False
    try:
        if OS_NAME == "Darwin":
            wrote_back = _mac_write_keychain(blob)
        elif OS_NAME == "Windows":
            if os.path.exists(CLAUDE_FILE_CREDS_PATH):
                tmp = CLAUDE_FILE_CREDS_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(blob)
                os.replace(tmp, CLAUDE_FILE_CREDS_PATH)
                wrote_back = True
            else:
                target, _ = _win_credread()
                wrote_back = _win_credwrite(target or CLAUDE_WIN_CRED_TARGETS[0], blob)
        else:  # Linux
            fd = os.open(CLAUDE_FILE_CREDS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(blob)
            wrote_back = True
    except Exception:
        wrote_back = False
    if wrote_back:
        # 官方儲存位置寫回成功,備援檔即失效
        if os.path.exists(CLAUDE_FALLBACK_CREDS):
            os.remove(CLAUDE_FALLBACK_CREDS)
        return
    # 寫回失敗:留一份 600 權限的備援檔,避免遺失還有效的 refresh token
    fd = os.open(CLAUDE_FALLBACK_CREDS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(blob)


# 端點會隨 Anthropic 改版搬家,依序嘗試
CLAUDE_TOKEN_ENDPOINTS = [
    "https://claude.ai/v1/oauth/token",
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
]


def claude_refresh(creds):
    oauth = creds["claudeAiOauth"]
    form = {
        "grant_type": "refresh_token",
        "client_id": CLAUDE_CLIENT_ID,
        "refresh_token": oauth["refreshToken"],
    }
    last = None
    for url in CLAUDE_TOKEN_ENDPOINTS:
        status, payload = http_json("POST", url, headers={"User-Agent": CLAUDE_UA}, form=form)
        if status == 200 and "access_token" in payload:
            break
        last = (url, status, payload.get("error"))
    else:
        url, status, err = last
        if status == 0:
            raise RuntimeError("網路連不上,請確認電腦已連上網際網路")
        if err in ("invalid_grant", "invalid_request") or status in (400, 401):
            raise RuntimeError("Claude 登入已失效,請重新開啟 Claude Code 登入一次")
        raise RuntimeError(f"Claude token refresh 失敗:{url} → HTTP {status} {err}")
    oauth["accessToken"] = payload["access_token"]
    if payload.get("refresh_token"):
        oauth["refreshToken"] = payload["refresh_token"]
    oauth["expiresAt"] = int((time.time() + payload.get("expires_in", 36000)) * 1000)
    claude_write_creds(creds)
    return creds


def claude_fetch():
    creds = claude_read_creds()
    if not creds or "claudeAiOauth" not in creds:
        return {"ok": False, "error": "找不到 Claude Code 憑證,請先開啟 Claude Code 登入一次"}
    oauth = creds["claudeAiOauth"]
    if oauth.get("expiresAt", 0) / 1000 < time.time() + 60:
        creds = claude_refresh(creds)
        oauth = creds["claudeAiOauth"]

    status, payload = http_json(
        "GET", "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {oauth['accessToken']}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": CLAUDE_UA,
        },
    )
    if status == 401:
        # access token 提前失效:強制刷新後重試一次
        creds = claude_refresh(creds)
        oauth = creds["claudeAiOauth"]
        status, payload = http_json(
            "GET", "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {oauth['accessToken']}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": CLAUDE_UA,
            },
        )
    if status != 200:
        if status == 0:
            return {"ok": False, "error": "網路連不上,請確認電腦已連上網際網路"}
        return {"ok": False, "error": f"Claude usage API HTTP {status}"}

    windows = []
    for lim in payload.get("limits") or []:
        if lim.get("percent") is None:
            continue
        kind = lim.get("kind", "")
        if kind == "session":
            wid, label = "5h", "5 小時"
        elif kind == "weekly_all":
            wid, label = "weekly", "每週"
        else:
            model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
            wid = kind
            label = f"每週 {model}" if model else kind
        windows.append({
            "id": wid,
            "label": label,
            "used_percent": round(float(lim["percent"]), 1),
            "resets_at": iso_to_epoch(lim.get("resets_at")) if lim.get("resets_at") else None,
        })
    if not windows:  # 舊版 schema 備援
        for key, wid, label in (("five_hour", "5h", "5 小時"), ("seven_day", "weekly", "每週")):
            val = payload.get(key)
            if isinstance(val, dict) and val.get("utilization") is not None:
                windows.append({
                    "id": wid,
                    "label": label,
                    "used_percent": round(float(val["utilization"]), 1),
                    "resets_at": iso_to_epoch(val["resets_at"]) if val.get("resets_at") else None,
                })
    order = {"5h": 0, "weekly": 1}
    windows.sort(key=lambda w: order.get(w["id"], 9))
    return {"ok": True, "plan": oauth.get("subscriptionType", ""), "windows": windows}


# ----------------------------------------------------------------- Codex ----

def jwt_exp(token):
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg)).get("exp", 0)
    except Exception:
        return 0


def codex_read_auth():
    with open(CODEX_AUTH_PATH) as f:
        return json.load(f)


def codex_refresh(auth):
    tokens = auth["tokens"]
    status, payload = http_json(
        "POST", "https://auth.openai.com/oauth/token",
        headers={"User-Agent": CODEX_UA},
        body={
            "client_id": CODEX_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "scope": "openid profile email",
        },
    )
    if status != 200 or "access_token" not in payload:
        if status == 0:
            raise RuntimeError("網路連不上,請確認電腦已連上網際網路")
        if payload.get("error") in ("invalid_grant", "invalid_request") or status in (400, 401):
            raise RuntimeError("Codex 登入已失效,請重新開啟 Codex App 登入一次")
        raise RuntimeError(f"Codex token refresh 失敗 (HTTP {status})")
    tokens["access_token"] = payload["access_token"]
    if payload.get("id_token"):
        tokens["id_token"] = payload["id_token"]
    if payload.get("refresh_token"):
        tokens["refresh_token"] = payload["refresh_token"]
    auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
    tmp = CODEX_AUTH_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(auth, f, indent=2)
    os.replace(tmp, CODEX_AUTH_PATH)
    return auth


def codex_fetch():
    if not os.path.exists(CODEX_AUTH_PATH):
        return {"ok": False, "error": "找不到 ~/.codex/auth.json,請先開啟 Codex 登入一次"}
    try:
        auth = codex_read_auth()
    except (json.JSONDecodeError, OSError):
        return {"ok": False, "error": "auth.json 格式損毀,請重新開啟 Codex 登入一次"}
    tokens = auth.get("tokens") or {}
    if not tokens.get("access_token"):
        return {"ok": False, "error": "auth.json 內沒有 access_token,請重新開啟 Codex 登入一次"}
    if jwt_exp(tokens["access_token"]) < time.time() + 60:
        auth = codex_refresh(auth)
        tokens = auth["tokens"]

    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "chatgpt-account-id": tokens.get("account_id", ""),
        "User-Agent": CODEX_UA,
    }
    status, payload = http_json("GET", "https://chatgpt.com/backend-api/wham/usage", headers=headers)
    if status == 401:
        auth = codex_refresh(auth)
        headers["Authorization"] = f"Bearer {auth['tokens']['access_token']}"
        status, payload = http_json("GET", "https://chatgpt.com/backend-api/wham/usage", headers=headers)
    if status != 200:
        if status == 0:
            return {"ok": False, "error": "網路連不上,請確認電腦已連上網際網路"}
        return {"ok": False, "error": f"Codex usage API HTTP {status}"}

    windows = []
    rl = payload.get("rate_limit") or {}
    for key, wid, label in (("primary_window", "5h", "5 小時"),
                            ("secondary_window", "weekly", "每週")):
        w = rl.get(key)
        if not w:
            continue
        windows.append({
            "id": wid,
            "label": label,
            "used_percent": round(float(w.get("used_percent", 0)), 1),
            "resets_at": w.get("reset_at"),
        })
    return {"ok": True, "plan": payload.get("plan_type", ""), "windows": windows}


# ----------------------------------------------------------------- 共用 ----

def iso_to_epoch(s):
    from datetime import datetime, timezone
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


class Store:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.lock = threading.Lock()
        con = self._con()
        con.execute("""CREATE TABLE IF NOT EXISTS history (
            ts INTEGER NOT NULL,
            provider TEXT NOT NULL,
            window_id TEXT NOT NULL,
            used_percent REAL NOT NULL,
            resets_at INTEGER
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_hist ON history (provider, window_id, ts)")
        con.commit()
        con.close()

    def _con(self):
        return sqlite3.connect(DB_PATH)

    def record(self, provider, result):
        if not result.get("ok"):
            return
        ts = int(time.time())
        with self.lock:
            con = self._con()
            for w in result["windows"]:
                con.execute(
                    "INSERT INTO history VALUES (?,?,?,?,?)",
                    (ts, provider, w["id"], w["used_percent"], w.get("resets_at")),
                )
            con.commit()
            con.close()

    def history(self, hours):
        since = int(time.time()) - hours * 3600
        with self.lock:
            con = self._con()
            rows = con.execute(
                "SELECT ts, provider, window_id, used_percent FROM history WHERE ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
            con.close()
        out = {}
        for ts, provider, wid, pct in rows:
            out.setdefault(f"{provider}:{wid}", []).append([ts, pct])
        return out


class Monitor:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.state = {"claude": None, "codex": None, "updated_at": None}

    def fetch_all(self):
        results = {}
        for name, fn in (("claude", claude_fetch), ("codex", codex_fetch)):
            try:
                results[name] = fn()
            except Exception as e:
                results[name] = {"ok": False, "error": str(e)}
            self.store.record(name, results[name])
        with self.lock:
            self.state = {**results, "updated_at": int(time.time())}
        return self.snapshot()

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def maybe_fetch(self):
        snap = self.snapshot()
        if snap["updated_at"] and time.time() - snap["updated_at"] < MIN_FETCH_INTERVAL:
            return snap
        return self.fetch_all()

    def poll_forever(self):
        while True:
            try:
                self.fetch_all()
            except Exception as e:
                print(f"[poll] error: {e}")
            time.sleep(POLL_SECONDS)


store = Store()
monitor = Monitor(store)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    STATIC_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/manifest+json; charset=utf-8",
        ".png": "image/png",
    }

    def _send_static(self, rel):
        full = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
            self._send(404, {"error": "not found"})
            return
        ctype = self.STATIC_TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path == "/":
            self._send_static("index.html")
        elif path in ("/manifest.json", "/sw.js") or path.startswith("/icons/"):
            self._send_static(path.lstrip("/"))
        elif path == "/ca.pem":
            # 只公開 CA 憑證(公鑰),方便手機下載安裝;私鑰不經過任何路由
            ca = os.path.join(CERT_DIR, "ca.pem")
            if os.path.isfile(ca):
                with open(ca, "rb") as f:
                    self._send(200, f.read(), "application/x-x509-ca-cert")
            else:
                self._send(404, {"error": "尚未產生憑證,先執行 uv run gen_certs.py"})
        elif path == "/api/usage":
            if params.get("fresh") == "1":
                self._send(200, monitor.maybe_fetch())
            else:
                self._send(200, monitor.snapshot())
        elif path == "/api/history":
            hours = min(int(params.get("hours", "24")), 24 * 30)
            self._send(200, store.history(hours))
        else:
            self._send(404, {"error": "not found"})


def lan_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    threading.Thread(target=monitor.poll_forever, daemon=True).start()
    ip = lan_ip()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Token Monitor 已啟動:")
    print(f"  本機   → http://localhost:{PORT}")
    print(f"  手機   → http://{ip}:{PORT}  (需同一個 Wi-Fi)")

    # certs/ 有憑證就加開 HTTPS(手機 PWA / service worker 需要 secure context)
    cert, key = os.path.join(CERT_DIR, "server.pem"), os.path.join(CERT_DIR, "server.key")
    if os.path.isfile(cert) and os.path.isfile(key):
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        tls_server = ThreadingHTTPServer(("0.0.0.0", TLS_PORT), Handler)
        tls_server.socket = ctx.wrap_socket(tls_server.socket, server_side=True)
        threading.Thread(target=tls_server.serve_forever, daemon=True).start()
        print(f"  HTTPS  → https://{ip}:{TLS_PORT}  (PWA 安裝用;CA 憑證: http://{ip}:{PORT}/ca.pem)")
    else:
        print(f"  (未偵測到 certs/,只跑 HTTP。要讓手機 PWA 完整運作請先執行 uv run gen_certs.py)")

    server.serve_forever()


if __name__ == "__main__":
    main()
