#!/usr/bin/env python3
"""產生自簽 CA 與伺服器憑證,讓手機走 HTTPS 使用 Token Monitor(PWA 需要)。

- certs/ca.pem + ca.key   : 本機自簽 CA(有效 10 年),ca.pem 要安裝到手機
- certs/server.pem + .key : 伺服器憑證(有效 825 天,Apple 對使用者信任憑證的上限),
                            SAN 涵蓋 localhost、<主機名>.local 與目前的區網 IP

區網 IP 變了(換 Wi-Fi、DHCP 重配)重跑一次即可,CA 不變、手機不用重裝。
"""
import os
import socket
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
CERT_DIR = os.path.join(BASE, "certs")
CA_KEY = os.path.join(CERT_DIR, "ca.key")
CA_PEM = os.path.join(CERT_DIR, "ca.pem")
SRV_KEY = os.path.join(CERT_DIR, "server.key")
SRV_PEM = os.path.join(CERT_DIR, "server.pem")


def run(*args):
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"指令失敗:{' '.join(args)}\n{r.stderr}")


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def mdns_name():
    try:
        out = subprocess.run(["scutil", "--get", "LocalHostName"],
                             capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip() + ".local"
    except Exception:
        pass
    host = socket.gethostname()
    return host if host.endswith(".local") else host + ".local"


def main():
    os.makedirs(CERT_DIR, exist_ok=True)
    os.chmod(CERT_DIR, 0o700)

    if not (os.path.exists(CA_KEY) and os.path.exists(CA_PEM)):
        print("建立 CA …")
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", "3650", "-keyout", CA_KEY, "-out", CA_PEM,
            "-subj", "/CN=Token Monitor Local CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    else:
        print("沿用既有 CA")

    ip = lan_ip()
    sans = ["DNS:localhost", f"DNS:{mdns_name()}", "IP:127.0.0.1"]
    if ip:
        sans.append(f"IP:{ip}")
    san_line = ",".join(sans)
    print(f"伺服器憑證 SAN:{san_line}")

    csr = os.path.join(CERT_DIR, "server.csr")
    ext = os.path.join(CERT_DIR, "san.cnf")
    with open(ext, "w") as f:
        f.write(f"subjectAltName={san_line}\n"
                "basicConstraints=CA:FALSE\n"
                "extendedKeyUsage=serverAuth\n")
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes",
        "-keyout", SRV_KEY, "-out", csr, "-subj", "/CN=Token Monitor")
    run("openssl", "x509", "-req", "-in", csr, "-CA", CA_PEM, "-CAkey", CA_KEY,
        "-CAcreateserial", "-days", "825", "-out", SRV_PEM, "-extfile", ext)
    os.remove(csr)
    os.remove(ext)
    for p in (CA_KEY, SRV_KEY):
        os.chmod(p, 0o600)

    print(f"""
完成!檔案在 {CERT_DIR}/

下一步:
1. 啟動 server(會自動偵測憑證並加開 HTTPS)
2. 手機瀏覽器開 http://{ip or '<區網IP>'}:8787/ca.pem 下載 CA 憑證並安裝:
   - iOS:設定 → 一般 → VPN 與裝置管理 → 安裝描述檔,
          再到 一般 → 關於本機 → 憑證信任設定 → 開啟完全信任
   - Android:設定 → 安全性 → 更多安全性設定 → 安裝憑證 → CA 憑證
3. 之後手機一律用 https://{ip or '<區網IP>'}:8788 開啟
""")


if __name__ == "__main__":
    main()
