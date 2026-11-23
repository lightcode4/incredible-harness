"""Computer use: the agent sees the screen and controls the mouse and keyboard (off by default; every action
asks for approval). Screenshots are scaled down to at most 1280 px wide; coordinates the model gives are in
screenshot space and are scaled back to real screen points here.

macOS: the app needs Screen Recording and Accessibility permission (System Settings > Privacy & Security).
"""

from __future__ import annotations

import base64
import io

MAX_W = 1280


class ComputerError(Exception):
    pass


class Computer:
    def __init__(self):
        self.scale = 1.0  # real screen points per screenshot pixel

    def _libs(self):
        try:
            import mss
            import pyautogui
            from PIL import Image
        except ImportError as e:
            raise ComputerError(f"Computer use is not available in this build ({e.name} missing).")
        pyautogui.FAILSAFE = True  # slam the mouse into a screen corner to stop it
        pyautogui.PAUSE = 0.15
        return mss, pyautogui, Image

    def screenshot(self) -> tuple[str, int, int]:
        mss, pyautogui, Image = self._libs()
        with mss.mss() as sct:
            mon = sct.monitors[1]
            raw = sct.grab(mon)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        points_w, _ = pyautogui.size()  # logical points (differs from pixels on Retina screens)
        w = min(MAX_W, img.width)
        h = round(img.height * w / img.width)
        img = img.resize((w, h))
        self.scale = points_w / w
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return base64.b64encode(buf.getvalue()).decode(), w, h

    def _pt(self, x: int, y: int) -> tuple[int, int]:
        return round(x * self.scale), round(y * self.scale)

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> str:
        _, pg, _ = self._libs()
        px, py = self._pt(x, y)
        pg.click(px, py, clicks=2 if double else 1, button=button)
        return f"{'Double-c' if double else 'C'}licked {button} at ({x}, {y})."

    def type(self, text: str) -> str:
        _, pg, _ = self._libs()
        pg.write(text, interval=0.01)
        return f"Typed {len(text)} characters."

    def key(self, keys: str) -> str:
        _, pg, _ = self._libs()
        parts = [k.strip().lower() for k in keys.replace("cmd", "command").split("+") if k.strip()]
        pg.hotkey(*parts)
        return f"Pressed {keys}."

    def scroll(self, dy: int, x: int | None = None, y: int | None = None) -> str:
        _, pg, _ = self._libs()
        if x is not None and y is not None:
            pg.moveTo(*self._pt(x, y))
        pg.scroll(-int(dy / 10))
        return f"Scrolled {dy}."
