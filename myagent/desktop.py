"""Incredible Herness desktop app: a native window (macOS WebKit / Windows Edge WebView2)
around the local engine. This is the entry point PyInstaller packages."""

from __future__ import annotations

import sys
from pathlib import Path

import webview

from .gui import start_server

APP_NAME = "Incredible Herness"


class Bridge:
    """Functions the page can call through window.pywebview.api."""

    def pick_folder(self):
        win = webview.windows[0]
        dialog = getattr(webview, "FileDialog", None)
        kind = dialog.FOLDER if dialog else webview.FOLDER_DIALOG
        result = win.create_file_dialog(kind)
        return result[0] if result else None


def main():
    # A packaged windowed app has no console; give print() somewhere harmless to go.
    import os
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    # Apps opened from Finder / Start menu start in "/" or System32; use the home folder instead.
    cwd = str(Path.home())
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_dir():
        cwd = sys.argv[1]
    server, url, session = start_server(cwd)
    session.preview_opener = lambda target: webview.create_window(f"Preview — {target}", target, width=1100, height=760)
    webview.create_window(APP_NAME, url, js_api=Bridge(), width=1180, height=820, min_size=(720, 520))
    webview.start()
    if session.ws:
        session.ws.stop_all_jobs()
    server.shutdown()


if __name__ == "__main__":
    main()
