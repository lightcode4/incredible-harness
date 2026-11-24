"""LightCode⁴ desktop app: a native window (macOS WebKit / Windows Edge WebView2)
around the local engine. This is the entry point PyInstaller packages."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import webview

from .gui import start_server

APP_NAME = "LightCode⁴"


class Bridge:
    """Functions the page can call through window.pywebview.api."""

    def pick_folder(self):
        win = webview.windows[0]
        dialog = getattr(webview, "FileDialog", None)
        kind = dialog.FOLDER if dialog else webview.FOLDER_DIALOG
        result = win.create_file_dialog(kind)
        return result[0] if result else None


DEFAULT_HOTKEY = "<ctrl>+<alt>+<space>"


def mac_extras(session, focus_main):
    """macOS only: a number on the Dock icon for waiting approvals, and notifications that open their chat when clicked."""
    if sys.platform != "darwin":
        return
    try:
        import AppKit
        import Foundation
        from PyObjCTools import AppHelper
    except Exception:
        return

    def set_badge(n: int):
        AppHelper.callAfter(lambda: AppKit.NSApp.dockTile().setBadgeLabel_(str(n) if n else ""))
    session.set_badge = set_badge

    center = Foundation.NSUserNotificationCenter.defaultUserNotificationCenter() if hasattr(Foundation, "NSUserNotificationCenter") else None
    if center is None:  # not running as an app bundle: keep the simple notifications
        return

    class Delegate(Foundation.NSObject):
        def userNotificationCenter_shouldPresentNotification_(self, c, n):
            return True

        def userNotificationCenter_didActivateNotification_(self, c, n):
            info = n.userInfo() or {}
            chat = info.get("chat")
            try:
                if chat and (session.current is None or session.current.id != chat):
                    session.open_chat(str(chat))
            except Exception:
                pass
            focus_main()
            AppKit.NSApp.activateIgnoringOtherApps_(True)

    delegate = Delegate.alloc().init()
    center.setDelegate_(delegate)
    session._notify_delegate = delegate  # keep it alive

    def notifier(title, message, chat=None):
        def show():
            n = Foundation.NSUserNotification.alloc().init()
            n.setTitle_(title)
            n.setInformativeText_(message[:240])
            if chat:
                n.setUserInfo_({"chat": chat})
            center.deliverNotification_(n)
        AppHelper.callAfter(show)
        return True
    from . import extras
    extras.NOTIFIER = notifier


def forward_link(argv: list[str]) -> bool:
    """A clicked notification starts the app with lightcode4://chat/<id>. If LightCode4 is already running, hand the
    chat to it (it opens the chat and comes to the front) and let this second copy exit."""
    link = next((a for a in argv[1:] if a.lower().startswith("lightcode4://")), None)
    if not link:
        return False
    import json
    import urllib.request
    from .gui import read_running
    info = read_running()
    chat = link.split("://", 1)[1].strip("/")
    chat = chat[5:] if chat.startswith("chat/") else ""
    if info:
        try:
            req = urllib.request.Request(f"{info['url']}/api/activate?token={info['token']}", data=json.dumps({"chat": chat}).encode(),
                                         headers={"Content-Type": "application/json", "X-Token": info["token"]}, method="POST")
            urllib.request.urlopen(req, timeout=5).read()
            return True
        except Exception:
            pass
    sys.argv = [a for a in argv if a != link]  # not running: start normally (the last chat reopens)
    return False


def win_extras(session, main_win):
    """Windows: the number of approvals waiting shows on the taskbar icon and in the title, and the taskbar button
    flashes, like the Dock badge on a Mac."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                    ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]

    def hwnd():
        try:
            return int(main_win.native.Handle.ToInt64())
        except Exception:
            return user32.FindWindowW(None, main_win.title) or 0

    def overlay(h, n):
        """A small numbered dot on the taskbar icon (ITaskbarList3.SetOverlayIcon)."""
        import tempfile
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)
        clsid, iid = (ctypes.c_byte * 16)(), (ctypes.c_byte * 16)()
        ole32.CLSIDFromString("{56FDF344-FD6D-11d0-958A-006097C9A090}", ctypes.byref(clsid))
        ole32.IIDFromString("{ea1afb91-9e28-4b86-90e9-9e9f8a5eefaf}", ctypes.byref(iid))
        obj = ctypes.c_void_p()
        if ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(obj)) != 0:
            return
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[3])(obj)  # HrInit
        icon = None
        if n:
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((0, 0, 31, 31), fill=(232, 196, 106, 255))
            t = str(n) if n < 10 else "9+"
            d.text((16, 16), t, fill=(43, 32, 18, 255), anchor="mm", font_size=20 if n < 10 else 15)
            path = os.path.join(tempfile.gettempdir(), f"lc4-badge-{t.replace('+', 'p')}.ico")
            img.save(path, sizes=[(32, 32), (16, 16)])
            icon = user32.LoadImageW(None, path, 1, 16, 16, 0x10)
        set_overlay = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND, wintypes.HANDLE, wintypes.LPCWSTR)(vtbl[18])
        set_overlay(obj, h, icon, f"{n} waiting for your approval" if n else "")
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)  # Release

    def set_badge(n: int):
        try:
            main_win.set_title(f"({n}) {APP_NAME}" if n else APP_NAME)
            h = hwnd()
            if not h:
                return
            if n:
                info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), h, 0x2 | 0xC, 0, 0)  # flash the taskbar button until the window is used
                user32.FlashWindowEx(ctypes.byref(info))
            try:
                overlay(h, n)
            except Exception:
                pass  # the title count and the flash still show it
        except Exception:
            pass
    session.set_badge = set_badge


def hotkey_for_pynput(text: str) -> str:
    """'ctrl+alt+space' / '⌃⌥Space' / '<ctrl>+<alt>+<space>' -> pynput's '<ctrl>+<alt>+<space>'."""
    t = (text or "").strip().lower()
    for a, b in (("⌃", "ctrl+"), ("⌥", "alt+"), ("⌘", "cmd+"), ("⇧", "shift+"), ("option", "alt"), ("control", "ctrl"),
                 ("command", "cmd"), ("<", ""), (">", ""), (" ", "")):
        t = t.replace(a, b)
    keys = [k for k in t.split("+") if k]
    named = {"ctrl", "alt", "cmd", "shift", "space", "enter", "tab", "esc"} | {f"f{i}" for i in range(1, 13)}
    return "+".join(f"<{k}>" if k in named else k for k in keys)


MAC_KEYCODES = {"space": 49, "enter": 36, "tab": 48, "esc": 53, **{f"f{i}": c for i, c in zip(range(1, 13),
                (122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111))}}
MAC_FLAGS = {"shift": 1 << 17, "ctrl": 1 << 18, "alt": 1 << 19, "cmd": 1 << 20}


def parse_hotkey(text: str) -> tuple[int, str]:
    """'<ctrl>+<alt>+<space>' -> (modifier flags, key name)."""
    mods, key = 0, ""
    for k in hotkey_for_pynput(text).replace("<", "").replace(">", "").split("+"):
        if k in MAC_FLAGS:
            mods |= MAC_FLAGS[k]
        elif k:
            key = k
    return mods, key


def hotkey_matches(mods: int, key: str, flags: int, keycode: int, chars: str) -> bool:
    if flags & sum(MAC_FLAGS.values()) != mods:
        return False
    return keycode == MAC_KEYCODES[key] if key in MAC_KEYCODES else (chars or "").lower() == key


class MacHotkey:
    """Global shortcut through Apple's own NSEvent monitors, run on the main thread. (pynput's keyboard listener
    builds key events on a background thread, which newer macOS punishes by killing the app, e.g. on Caps Lock.)"""

    def __init__(self, hotkey: str, callback):
        self.mods, self.key = parse_hotkey(hotkey)
        self.callback, self.monitors = callback, []
        import AppKit
        from PyObjCTools import AppHelper
        self.AppKit = AppKit
        AppHelper.callAfter(self._install)

    def _hit(self, ev) -> bool:
        try:
            return hotkey_matches(self.mods, self.key, int(ev.modifierFlags()), int(ev.keyCode()), ev.charactersIgnoringModifiers())
        except Exception:
            return False

    def _install(self):
        A = self.AppKit
        mask = getattr(A, "NSEventMaskKeyDown", 1 << 10)

        def global_handler(ev):
            if self._hit(ev):
                self.callback()

        def local_handler(ev):
            if self._hit(ev):
                self.callback()
                return None  # handled: don't type it into the page
            return ev
        self.monitors = [A.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, global_handler),
                         A.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, local_handler)]

    def stop(self):
        from PyObjCTools import AppHelper
        mons, self.monitors = self.monitors, []

        def remove():
            for m in mons:
                if m is not None:
                    self.AppKit.NSEvent.removeMonitor_(m)
        AppHelper.callAfter(remove)


class QuickChat:
    """A small always-on-top box, shown and hidden with a global keyboard shortcut (works while other apps are in front).
    macOS asks once for Input Monitoring permission (System Settings > Privacy & Security)."""

    def __init__(self, url: str, main_win):
        self.main_win, self.listener, self.shown = main_win, None, False
        sep = "&" if "?" in url else "?"
        self.win = webview.create_window(f"{APP_NAME} quick chat", url + sep + "quick=1", js_api=Bridge(), text_select=True, width=640, height=168,
                                         frameless=True, on_top=True, hidden=True, resizable=False, easy_drag=True)

    def toggle(self):
        self.hide() if self.shown else self.show()

    def show(self):
        self.shown = True
        try:
            self.win.show()
            self.win.evaluate_js("window.quickFocus && quickFocus()")
        except Exception:
            pass

    def hide(self):
        self.shown = False
        try:
            self.win.hide()
        except Exception:
            pass

    def focus_main(self):
        try:
            self.main_win.show()
            self.main_win.restore()
        except Exception:
            pass

    def listen(self, hotkey: str):
        self.stop()
        if not hotkey:
            return
        try:
            if sys.platform == "darwin":
                self.listener = MacHotkey(hotkey, self.toggle)
                return
            from pynput import keyboard
            self.listener = keyboard.GlobalHotKeys({hotkey_for_pynput(hotkey): self.toggle})
            self.listener.daemon = True
            self.listener.start()
        except Exception as e:  # no permission or no keyboard access: the rest of the app keeps working
            import logging
            logging.getLogger("lightcode4").warning("Quick chat shortcut unavailable: %s", e)

    def stop(self):
        if self.listener:
            try:
                self.listener.stop()
            except Exception:
                pass
            self.listener = None


def main():
    if forward_link(sys.argv):  # a notification was clicked while the app is running: it shows that chat; nothing else to do
        return
    if "--self-test" in sys.argv:  # test every part of the installed app, print results, exit
        from .selfcheck import main as selfcheck_main
        raise SystemExit(selfcheck_main())
    # A packaged windowed app has no console; give print() somewhere harmless to go.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    # Apps opened from Finder / Start menu start in "/" or System32; use the home folder instead.
    cwd = str(Path.home())
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_dir():
        cwd = sys.argv[1]
    server, url, session = start_server(cwd)
    windows = []

    def new_window():
        windows.append(webview.create_window(APP_NAME, url, js_api=Bridge(), text_select=True, width=1180, height=820, min_size=(720, 520)))

    def quit_app():
        for w in list(webview.windows):
            try:
                w.destroy()
            except Exception:
                pass
    session.new_window, session.quit_app = new_window, quit_app
    session.preview_opener = lambda target: webview.create_window(f"Preview — {target}", target, text_select=True, width=1100, height=760)
    main_win = webview.create_window(APP_NAME, url, js_api=Bridge(), text_select=True, width=1180, height=820, min_size=(720, 520))
    quick = QuickChat(url, main_win)
    session.quick_toggle, session.quick_hide, session.focus_main = quick.toggle, quick.hide, quick.focus_main
    session.on_hotkey = quick.listen
    mac_extras(session, quick.focus_main)
    win_extras(session, main_win)
    webview.start(lambda: quick.listen(session.config.get("quick_hotkey", DEFAULT_HOTKEY)))
    quick.stop()
    session.shutdown()
    server.shutdown()


if __name__ == "__main__":
    main()
