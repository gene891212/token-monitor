#!/usr/bin/env python3
"""macOS menu bar app for Token Monitor."""

import sys
import threading
import time

import rumps
from AppKit import NSAttributedString, NSFont, NSFontAttributeName
from PyObjCTools import AppHelper

import server

MENU_INDENT = "      "


def set_bold_title(item, title):
    item.title = title
    font = NSFont.boldSystemFontOfSize_(NSFont.systemFontSize())
    attrs = {NSFontAttributeName: font}
    item._menuitem.setAttributedTitle_(
        NSAttributedString.alloc().initWithString_attributes_(title, attrs)
    )


def percent_for(result, window_id):
    if not result or not result.get("ok"):
        return None
    for window in result.get("windows", []):
        if window.get("id") == window_id:
            return window.get("used_percent")
    return None


def first_percent(result):
    """取得 provider 結果中第一個視窗的使用百分比."""
    if not result or not result.get("ok"):
        return None
    windows = result.get("windows", [])
    return windows[0].get("used_percent") if windows else None


def short_pct(value):
    if value is None:
        return "--"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"


def fmt_time(ts):
    if not ts:
        return ""
    return time.strftime("%m/%d %H:%M", time.localtime(ts))


def fmt_countdown(ts):
    if not ts:
        return ""
    seconds = int(ts - time.time())
    if seconds <= 0:
        return "即將重置"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    abs_time = time.strftime("%m/%d %H:%M", time.localtime(ts))

    if days:
        left = f"{days}d {hours}h" if hours > 0 else f"{days}d"
    elif hours:
        left = f"{hours}h {minutes}m" if minutes > 0 else f"{hours}h"
    else:
        left = f"{minutes}m"
    return f"{abs_time} | {left} left"


def window_line(window):
    pct = short_pct(window.get("used_percent"))
    reset = fmt_countdown(window.get("resets_at"))
    if reset:
        return f"{window.get('label', window.get('id'))}: {pct}% | {reset}"
    return f"{window.get('label', window.get('id'))}: {pct}%"


def child_line(text):
    return f"{MENU_INDENT}{text}" if text else ""


class TokenMonitorMenu(rumps.App):
    def __init__(self):
        super().__init__("Tokens --", quit_button=None)
        self._fetching = False

        self.updated_item = rumps.MenuItem("尚未更新")
        self.claude_header = rumps.MenuItem("Claude")
        self.claude_open = rumps.MenuItem("開啟Claude", callback=self.open_claude)
        self.claude_5h = rumps.MenuItem(child_line("--"))
        self.claude_weekly = rumps.MenuItem(child_line(""))
        self.claude_extra = rumps.MenuItem("")
        self.codex_header = rumps.MenuItem("Codex")
        self.codex_open = rumps.MenuItem("開啟Codex", callback=self.open_codex)
        self.codex_5h = rumps.MenuItem(child_line("--"))
        self.codex_weekly = rumps.MenuItem(child_line(""))
        self.codex_extra = rumps.MenuItem("")
        self.refresh_item = rumps.MenuItem("刷新", callback=self.refresh_now)
        self.quit_item = rumps.MenuItem("退出", callback=self.quit_app)

        self.menu = [
            self.refresh_item,
            None,
            self.claude_header,
            self.claude_5h,
            self.claude_weekly,
            self.claude_extra,
            None,
            self.codex_header,
            self.codex_5h,
            self.codex_weekly,
            self.codex_extra,
            None,
            self.updated_item,
            None,
            self.claude_open,
            self.codex_open,
            None,
            self.quit_item,
        ]
        for item in (
            self.updated_item,
            self.claude_header,
            self.claude_5h,
            self.claude_weekly,
            self.claude_extra,
            self.codex_header,
            self.codex_5h,
            self.codex_weekly,
            self.codex_extra,
        ):
            item.set_callback(None)

        set_bold_title(self.claude_header, self.claude_header.title)
        set_bold_title(self.codex_header, self.codex_header.title)
        self.claude_extra.hide()
        self.codex_extra.hide()

        self.timer = rumps.Timer(self.refresh_if_idle, 60)
        self.timer.start()
        self.refresh_if_idle(None)

    def _set_title_from_state(self, state):
        claude_pct = first_percent(state.get("claude"))
        codex_pct = first_percent(state.get("codex"))
        self.title = f"C: {short_pct(claude_pct)}% X: {short_pct(codex_pct)}%"

    def _provider_lines(self, label, result):
        if not result:
            return (label, child_line("尚無資料"), "", "")
        if not result.get("ok"):
            return (
                label,
                child_line(f"讀取失敗: {result.get('error', '讀取失敗')}"),
                "",
                "",
            )

        plan = result.get("plan")
        header = f"{label} ({plan})" if plan else label
        wins = result.get("windows", [])
        primary = child_line(window_line(wins[0])) if len(wins) > 0 else ""
        secondary = child_line(window_line(wins[1])) if len(wins) > 1 else ""
        extras = [window_line(w) for w in wins[2:]]
        return (
            header,
            primary,
            secondary,
            child_line(" | ".join(extras)),
        )

    def _apply_provider(self, provider, label, result):
        header, five_hour, weekly, extra = self._provider_lines(label, result)
        set_bold_title(getattr(self, f"{provider}_header"), header)
        getattr(self, f"{provider}_5h").title = five_hour
        getattr(self, f"{provider}_weekly").title = weekly
        extra_item = getattr(self, f"{provider}_extra")
        extra_item.title = extra
        if extra:
            extra_item.show()
        else:
            extra_item.hide()

    def _apply_state(self, state, fresh):
        self._set_title_from_state(state)
        self._apply_provider("claude", "Claude", state.get("claude"))
        self._apply_provider("codex", "Codex", state.get("codex"))
        if state.get("updated_at"):
            updated = time.strftime("%H:%M:%S", time.localtime(state["updated_at"]))
            self.updated_item.title = f"更新於 {updated}"
        else:
            self.updated_item.title = "尚未更新"
        self.refresh_item.title = "刷新"
        self._fetching = False
        if fresh:
            rumps.notification("Token Monitor", "", "已更新用量")

    def _apply_error(self, error):
        self.title = "Tokens !"
        self.updated_item.title = f"讀取失敗: {error}"
        self.refresh_item.title = "刷新"
        self._fetching = False

    def refresh_if_idle(self, _):
        if self._fetching:
            return
        self._fetching = True
        self.refresh_item.title = "刷新中..."
        threading.Thread(target=self._fetch_worker, args=(False,), daemon=True).start()

    def refresh_now(self, _):
        if self._fetching:
            return
        self._fetching = True
        self.refresh_item.title = "刷新中..."
        threading.Thread(target=self._fetch_worker, args=(True,), daemon=True).start()

    def _fetch_worker(self, fresh):
        try:
            state = (
                server.monitor.fetch_all() if fresh else server.monitor.maybe_fetch()
            )
            AppHelper.callAfter(self._apply_state, state, fresh)
        except Exception as exc:
            AppHelper.callAfter(self._apply_error, str(exc))

    def quit_app(self, _):
        rumps.quit_application()

    def open_claude(self, _):
        import subprocess

        res = subprocess.run(["open", "-a", "Claude"], capture_output=True, text=True)
        if res.returncode != 0:
            rumps.notification("Token Monitor", "無法開啟 Claude", res.stderr.strip())

    def open_codex(self, _):
        import subprocess

        res = subprocess.run(["open", "-a", "Codex"], capture_output=True, text=True)
        if res.returncode != 0:
            rumps.notification("Token Monitor", "無法開啟 Codex", res.stderr.strip())


if __name__ == "__main__":
    if sys.platform != "darwin":
        raise SystemExit("menu_bar.py 只支援 macOS 狀態列。")
    TokenMonitorMenu().run()
