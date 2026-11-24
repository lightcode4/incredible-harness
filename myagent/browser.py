"""The built-in browser: LightCode⁴'s own Chromium engine (bundled with the app), driven by Playwright.

The agent controls it with browser_* tools (navigate, click, type, read, screenshot). The side panel shows a
live view (a screenshot stream) and forwards the user's clicks and typing. It has its own profile
(~/.incredible-herness/browser), separate from the user's everyday browsers.
All Playwright calls run on one dedicated thread, as Playwright's sync API requires.
"""

from __future__ import annotations

import base64
import hashlib
import os
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path

VIEWPORT = {"width": 1280, "height": 800}

# Lists visible clickable/typeable elements with their centre point, for browser_read.
_ELEMENTS_JS = """() => {
  const out = [];
  for (const el of document.querySelectorAll('a,button,input,select,textarea,[role=button],[role=link],[onclick],[contenteditable=true]')) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
    const s = getComputedStyle(el); if (s.visibility === 'hidden' || s.display === 'none') continue;
    const label = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.title || el.name || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    out.push(`${el.tagName.toLowerCase()}${el.type ? '[' + el.type + ']' : ''} "${label}" at (${Math.round(r.x + r.width / 2)}, ${Math.round(r.y + r.height / 2)})`);
    if (out.length >= 80) break;
  }
  return out;
}"""


class NeedsBrowser(Exception):
    pass


if getattr(sys, "frozen", False):
    # The packaged app ships its own Chromium inside the app bundle (installed there at build time).
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")


def install_chromium(progress=None) -> None:
    """Download the built-in Chromium (development builds only; packaged apps already include it)."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    driver = compute_driver_executable()
    cmd = [*driver, "install", "chromium-headless-shell"] if isinstance(driver, (tuple, list)) else [str(driver), "install", "chromium-headless-shell"]
    proc = subprocess.Popen(cmd, env=get_driver_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for line in proc.stdout:
        if progress and line.strip():
            progress(line.strip()[-120:])
    if proc.wait() != 0:
        raise RuntimeError("Chromium download failed.")


class Browser:
    def __init__(self, profile_dir: Path, on_frame, on_state):
        self.profile_dir = profile_dir
        self.on_frame, self.on_state = on_frame, on_state  # on_frame(jpeg_b64, w, h), on_state(dict)
        self.jobs: "queue.Queue[tuple]" = queue.Queue()
        self.thread: threading.Thread | None = None
        self.viewers = 0
        self.page = None
        self._last_hash = None
        self._last_shot = 0.0
        self.error: str | None = None

    # ---- thread plumbing ----
    def call(self, fn, timeout: float = 90):
        if not self.thread or not self.thread.is_alive():
            ready = Future()
            self.thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
            self.thread.start()
            ready.result(timeout=120)  # raises NeedsBrowser / launch errors
        fut = Future()
        self.jobs.put((fn, fut))
        return fut.result(timeout=timeout)

    def _run(self, ready: Future):
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as p:
                kwargs = dict(headless=True, viewport=VIEWPORT, args=["--no-first-run", "--no-default-browser-check"])
                if os.environ.get("HERNESS_BROWSER"):  # override for development and tests
                    kwargs["executable_path"] = os.environ["HERNESS_BROWSER"]
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                try:
                    ctx = p.chromium.launch_persistent_context(str(self.profile_dir), **kwargs)
                except Exception as e:
                    if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
                        raise NeedsBrowser("The built-in browser engine is not installed.")
                    raise
                self.page = ctx.pages[0] if ctx.pages else ctx.new_page()
                ctx.on("page", self._on_new_page)
                ready.set_result(True)
                self._state()
                while True:
                    try:
                        fn, fut = self.jobs.get(timeout=0.15)
                    except queue.Empty:
                        self._idle()
                        continue
                    if fn is None:
                        fut.set_result(True)
                        break
                    try:
                        fut.set_result(fn(self.page))
                    except Exception as e:
                        fut.set_exception(e)
                    self._state()
                    self._frame(force=True)
                ctx.close()
        except Exception as e:
            self.error = str(e)
            if not ready.done():
                ready.set_exception(e)
        finally:
            self.page = None

    def _on_new_page(self, page):
        self.page = page  # follow popups / new tabs

    def _idle(self):
        try:
            self.page.wait_for_timeout(50)  # lets Playwright process browser events
        except Exception:
            return
        if self.viewers > 0 and time.time() - self._last_shot > 0.5:
            self._frame()

    def _frame(self, force=False):
        if self.viewers <= 0 or not self.page:
            return
        try:
            shot = self.page.screenshot(type="jpeg", quality=60, timeout=3000)
        except Exception:
            return
        self._last_shot = time.time()
        h = hashlib.md5(shot).hexdigest()
        if force or h != self._last_hash:
            self._last_hash = h
            self.on_frame(base64.b64encode(shot).decode(), VIEWPORT["width"], VIEWPORT["height"])

    def _state(self):
        try:
            self.on_state({"url": self.page.url, "title": self.page.title()})
        except Exception:
            pass

    def close(self):
        if self.thread and self.thread.is_alive():
            fut = Future()
            self.jobs.put((None, fut))
            try:
                fut.result(timeout=10)
            except Exception:
                pass

    # ---- actions (run on the browser thread) ----
    @staticmethod
    def _settle(page):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(300)

    def navigate(self, url: str) -> str:
        if "://" not in url and not url.startswith(("about:", "file:")):
            url = ("http://" if url.startswith(("localhost", "127.0.0.1")) else "https://") + url

        def go(page):
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            self._settle(page)
            return f"Opened {page.url} — {page.title()}"
        return self.call(go)

    def history(self, action: str) -> str:
        def go(page):
            {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[action](timeout=15000)
            self._settle(page)
            return f"{page.url} — {page.title()}"
        return self.call(go)

    def screenshot(self) -> str:
        return self.call(lambda page: base64.b64encode(page.screenshot(type="jpeg", quality=70)).decode())

    def click(self, x: int | None = None, y: int | None = None, text: str | None = None, selector: str | None = None) -> str:
        def go(page):
            if selector:
                page.locator(selector).first.click(timeout=8000)
            elif text:
                page.get_by_text(text, exact=False).first.click(timeout=8000)
            elif x is not None and y is not None:
                page.mouse.click(x, y)
            else:
                raise ValueError("Give x and y, text, or selector.")
            self._settle(page)
            return f"Clicked. Now at {page.url} — {page.title()}"
        return self.call(go)

    def type(self, text: str, selector: str | None = None, submit: bool = False) -> str:
        def go(page):
            if selector:
                page.locator(selector).first.fill(text, timeout=8000)
            else:
                page.keyboard.insert_text(text)
            if submit:
                page.keyboard.press("Enter")
                self._settle(page)
            return f"Typed {len(text)} characters{' and pressed Enter' if submit else ''}. Now at {page.url}"
        return self.call(go)

    def press(self, key: str) -> str:
        def go(page):
            page.keyboard.press(key)
            self._settle(page)
            return f"Pressed {key}. Now at {page.url}"
        return self.call(go)

    def mouse(self, kind: str, x: int, y: int) -> None:
        """Mouse down / move / up / double-click from the Browser panel, so you can drag to select text."""
        def go(page):
            page.mouse.move(x, y)
            if kind == "down":
                page.mouse.down()
            elif kind == "up":
                page.mouse.up()
            elif kind == "dblclick":
                page.mouse.dblclick(x, y)
        self.call(go)

    def selection(self) -> str:
        """The text currently selected on the page (or in the focused text box)."""
        return self.call(lambda page: page.evaluate("""() => { const a = document.activeElement;
            if (a && /^(INPUT|TEXTAREA)$/.test(a.tagName) && a.selectionStart != null) return a.value.slice(a.selectionStart, a.selectionEnd);
            return String(getSelection() || ''); }""")) or ""

    def scroll(self, dy: int) -> str:
        def go(page):
            page.mouse.wheel(0, dy)
            page.wait_for_timeout(250)
            return f"Scrolled {dy}px."
        return self.call(go)

    def read(self) -> str:
        def go(page):
            text = page.inner_text("body", timeout=8000) if page.locator("body").count() else ""
            elements = page.evaluate(_ELEMENTS_JS)
            return (f"URL: {page.url}\nTitle: {page.title()}\n\nInteractive elements (centre x, y in a "
                    f"{VIEWPORT['width']}x{VIEWPORT['height']} viewport):\n" + "\n".join(elements)
                    + "\n\nPage text:\n" + text[:15000])
        return self.call(go)
