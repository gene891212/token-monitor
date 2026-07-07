# Token Monitor

監控 **Claude**(Desktop / 網頁 / Claude Code 共用額度)與 **Codex**(ChatGPT 訂閱額度)的
5 小時視窗與每週視窗使用率,提供手機可看的網頁儀表板。

額度是**帳號層級**共用的,所以就算主要用桌面版 App,這裡顯示的數字就是你實際的剩餘額度。

## 登入需求

- **Claude:**必須先安裝 Claude Code CLI 並完成登入。Token Monitor 目前只讀取 Claude Code
  的 OAuth 憑證,不會讀取 Claude Desktop 的登入資料。查到的是帳號共用額度,因此仍包含
  Claude Desktop、網頁版與 Claude Code 的使用量,但無法區分各來源。
- **Codex:**Codex Desktop App 或 Codex CLI 任一端完成登入即可,兩者共用
  `~/.codex/auth.json`。查到的同樣是帳號共用額度,無法區分 Desktop 與 CLI 的使用量。

## 啟動

專案用 [uv](https://docs.astral.sh/uv/) 管理,不用手動建 virtualenv 或裝套件
(本專案本來就只用 Python 標準庫,`uv` 主要負責鎖定 Python 版本、統一啟動方式)。

```sh
uv run server.py
```

第一次執行 `uv` 會自動建立 `.venv` 並安裝對應版本的 Python(見 `.python-version`),之後啟動秒開。

- 本機打開 <http://localhost:8787>
- 手機(同一個 Wi-Fi)打開 `http://<這台電腦的區網 IP>:8787`,啟動時終端機會印出網址
- 手機上可用 Safari / Chrome「加入主畫面」變成類 App 的體驗

## 開機自動啟動

**macOS(launchd)**

```sh
cp com.gene.token-monitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.gene.token-monitor.plist
```

停用:

```sh
launchctl unload ~/Library/LaunchAgents/com.gene.token-monitor.plist
```

**Windows(工作排程器)**

用系統管理員身分開 PowerShell:

```powershell
schtasks /create /tn "TokenMonitor" /tr "C:\path\to\token-monitor\start-windows.bat" /sc onlogon /rl highest
```

移除:

```powershell
schtasks /delete /tn "TokenMonitor" /f
```

## 資料來源

| 來源 | 憑證位置 | 端點 |
|---|---|---|
| Claude | macOS:Keychain 的 `Claude Code-credentials`;Windows / Linux:`~/.claude/.credentials.json`(Windows 另相容舊版 Credential Manager) | `api.anthropic.com/api/oauth/usage`(非公開端點,即 Claude Code `/usage` 指令的資料來源) |
| Codex | `~/.codex/auth.json`(三平台格式一致,Windows 為 `%USERPROFILE%\.codex\auth.json`) | `chatgpt.com/backend-api/wham/usage`(Codex `/status` 的資料來源) |

### Windows 注意事項

Windows Credential Manager 裡 Claude Code 憑證的 **target name** 沒有實機驗證過,程式預設依序嘗試
`Claude Code-credentials`、`Claude Code`。如果讀不到,打開「認證管理員」(Credential Manager)→
「Windows 認證」,找到 Claude 相關的項目,把正確名稱設定成環境變數再啟動:

```powershell
$env:TOKEN_MONITOR_CLAUDE_CRED_TARGET = "實際看到的名稱"
uv run server.py
```

Token 過期時會自動用 refresh token 換新,並寫回原本的儲存位置(Keychain / auth.json),
與官方工具的行為一致。Token 不會出現在日誌、瀏覽器或任何 HTTP 回應中。

## 注意事項

- 兩個用量端點都是**非官方文件化**的端點,格式可能隨官方改版而變;`server.py`
  對 Claude 的回應解析有新舊兩套 schema 的備援。
- 背景每 5 分鐘輪詢一次(社群經驗 180 秒以上是安全頻率),手動「刷新」最短間隔 60 秒。
- 歷史資料存在 `data/history.db`(SQLite),趨勢圖會隨時間累積。
- 若 Claude 憑證徹底失效(refresh token 也過期),開一次 Claude Code 重新登入即可。
