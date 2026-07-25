"""Per-browser X display + display-level human input.

Each :class:`~browser.session.LiveBrowser` gets its OWN :class:`Display`: a
dedicated ``Xvfb`` server plus a python-xlib connection used to inject the human's
mouse/keyboard via the XTest extension. Two reasons this is per-browser, not the
old shared ``:99``:

* **Fidelity.** XTest injects at the *display* level, so native right-click context
  menus, native ``<select>`` dropdowns / date pickers, and real click-drag all work
  -- none of which CDP's page-scoped ``Input.dispatch*`` can reach. The agent keeps
  driving over CDP (browser-use); only the *human* path moves to XTest.
* **Isolation.** One X11 CLIPBOARD per display means two browsers never clobber each
  other's clipboard (the latent shared-``:99`` cross-talk bug).

The display is started headful under Xvfb at a FIXED max framebuffer
(``_SCREEN_W`` x ``_SCREEN_H``); Xvfb cannot grow a framebuffer past its initial
size at runtime (verified), so "resize to the pane" shrinks the Chromium *window*
(CDP ``Browser.setWindowBounds``) and the capture region instead -- never the
framebuffer. See ``docs/live-view-v2.md``.

Input keycodes are resolved from the browser event's PHYSICAL ``code`` (e.g.
``KeyA``, ``Digit1``, ``Enter``), not its ``key``: XTest works at the keycode level
and modifier keys are replayed as their own events, so ``Shift`` held + the ``a``
keycode yields ``A`` naturally -- the VNC/neko approach -- with no shift-level
computation. Keysyms the running keymap lacks are bound to a spare keycode on the
fly (the TigerVNC trick) so any character can be typed.
"""

import asyncio
import os
import time
from pathlib import Path

from loguru import logger
from Xlib import X, XK
from Xlib import display as xdisplay
from Xlib import error as xerror
from Xlib.ext import xtest

# Fixed max framebuffer. Every browser's Xvfb starts here; the live view shrinks
# from this via window-bounds + capture-region, never by growing the framebuffer
# (Xvfb can't). Matches _RENDER_MAX_* in session.py; session reads SCREEN_H to bound
# the window height so it always fits.
SCREEN_W = int(os.environ.get("BROWSER_SCREEN_WIDTH", "1920"))
SCREEN_H = int(os.environ.get("BROWSER_SCREEN_HEIGHT", "1080"))

# Where to start allocating display numbers. Kept well clear of a workspace's own
# :0/:99 so a stray shared display never collides with a per-browser one.
_DISPLAY_BASE = int(os.environ.get("BROWSER_DISPLAY_BASE", "100"))

_XVFB_READY_TIMEOUT = float(os.environ.get("BROWSER_XVFB_READY_TIMEOUT", "10"))

# Display numbers currently owned by a live browser. Allocation happens under the
# manager's serialized launch, and both alloc/free run on the loop thread, so a
# plain set needs no lock.
_used_displays: set[int] = set()


def _alloc_display_num() -> int:
    """Pick a free display number: not already ours and with no leftover X socket."""
    num = _DISPLAY_BASE
    while num in _used_displays or Path(f"/tmp/.X11-unix/X{num}").exists():
        num += 1
    _used_displays.add(num)
    return num


def _free_display_num(num: int) -> None:
    _used_displays.discard(num)


class DisplayError(Exception):
    """Raised when the per-browser Xvfb can't be started."""


# Browser KeyboardEvent.code -> X keysym NAME for the PHYSICAL (unshifted) key.
# Letters/digits are filled in programmatically below. Only the keys whose code
# name differs from their keysym name are listed explicitly.
_CODE_TO_KEYSYM_NAME: dict[str, str] = {
    "Enter": "Return", "NumpadEnter": "Return", "Tab": "Tab", "Space": "space",
    "Backspace": "BackSpace", "Escape": "Escape", "Delete": "Delete", "Insert": "Insert",
    "Home": "Home", "End": "End", "PageUp": "Prior", "PageDown": "Next",
    "ArrowUp": "Up", "ArrowDown": "Down", "ArrowLeft": "Left", "ArrowRight": "Right",
    "ShiftLeft": "Shift_L", "ShiftRight": "Shift_R",
    "ControlLeft": "Control_L", "ControlRight": "Control_R",
    "AltLeft": "Alt_L", "AltRight": "Alt_R",
    "MetaLeft": "Super_L", "MetaRight": "Super_R",
    "CapsLock": "Caps_Lock",
    "Minus": "minus", "Equal": "equal", "BracketLeft": "bracketleft",
    "BracketRight": "bracketright", "Backslash": "backslash", "Semicolon": "semicolon",
    "Quote": "apostrophe", "Backquote": "grave", "Comma": "comma", "Period": "period",
    "Slash": "slash",
    "NumpadAdd": "KP_Add", "NumpadSubtract": "KP_Subtract", "NumpadMultiply": "KP_Multiply",
    "NumpadDivide": "KP_Divide", "NumpadDecimal": "KP_Decimal",
}
for _i in range(26):
    _CODE_TO_KEYSYM_NAME[f"Key{chr(ord('A') + _i)}"] = chr(ord("a") + _i)
for _i in range(10):
    _CODE_TO_KEYSYM_NAME[f"Digit{_i}"] = str(_i)
    _CODE_TO_KEYSYM_NAME[f"Numpad{_i}"] = f"KP_{_i}"
for _i in range(1, 13):
    _CODE_TO_KEYSYM_NAME[f"F{_i}"] = f"F{_i}"


def _keysym_for(code: str, key: str) -> int:
    """Resolve a browser (code, key) to an X keysym. Prefer the physical ``code``
    (layout-independent, so modifiers replay naturally); fall back to the produced
    character in ``key`` (covers dead keys / layouts our table misses)."""
    name = _CODE_TO_KEYSYM_NAME.get(code)
    if name is not None:
        keysym = XK.string_to_keysym(name)
        if keysym:
            return keysym
    if key and len(key) == 1:
        cp = ord(key)
        # Latin-1 keysyms equal the codepoint; other BMP chars use the Unicode plane.
        return cp if cp <= 0xFF else 0x01000000 + cp
    return 0


class Display:
    """One Xvfb server + XTest input connection for a single browser."""

    def __init__(self) -> None:
        self.num = _alloc_display_num()
        self.name = f":{self.num}"
        self._proc: asyncio.subprocess.Process | None = None
        self._x: xdisplay.Display | None = None
        self._root: object | None = None
        # Capture crop: input arrives in frame coords (the cropped capture region);
        # add this offset to reach true display coords. Set by session on measure.
        self.crop_x = 0
        self.crop_y = 0
        # Keysyms we've bound to a spare keycode (keysym -> keycode), so a key the
        # base keymap lacks can still be typed and its keyup matches its keydown.
        self._scratch: dict[int, int] = {}
        self._spare_keycode: int | None = None

    async def start(self) -> None:
        """Spawn Xvfb and open the XTest connection. Async so a ~1s server start
        never blocks the shared loop; readiness is polled off the X socket."""
        self._proc = await asyncio.create_subprocess_exec(
            "Xvfb", self.name,
            "-screen", "0", f"{SCREEN_W}x{SCREEN_H}x24",
            "-nolisten", "tcp", "+extension", "RANDR",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await self._await_ready()
        # python-xlib connect is sync but fast (local socket, one round-trip).
        self._x = xdisplay.Display(self.name)
        if self._x.query_extension("XTEST") is None:
            raise DisplayError(f"XTest extension missing on {self.name}")
        self._root = self._x.screen().root
        self._spare_keycode = self._x.display.info.max_keycode  # top of range: unused

    async def _await_ready(self) -> None:
        assert self._proc is not None
        sock = Path(f"/tmp/.X11-unix/X{self.num}")
        deadline = time.monotonic() + _XVFB_READY_TIMEOUT
        while time.monotonic() < deadline:
            if self._proc.returncode is not None:
                raise DisplayError(f"Xvfb {self.name} exited early (rc={self._proc.returncode})")
            if sock.exists():
                try:  # confirm we can actually open it, not just that the socket file exists
                    probe = xdisplay.Display(self.name)
                    probe.close()
                    return
                except (xerror.DisplayError, OSError, ConnectionError):
                    pass
            await asyncio.sleep(0.1)
        raise DisplayError(f"Xvfb {self.name} not ready within {_XVFB_READY_TIMEOUT:.0f}s")

    # --- input (XTest) --------------------------------------------------------
    # All calls are sync socket writes to the local Xvfb (sub-millisecond) and run
    # on the loop thread. Best-effort: a dead/racing connection is swallowed (the
    # browser may be tearing down); input is never load-bearing for correctness.

    def _sync(self) -> None:
        if self._x is not None:
            try:
                self._x.sync()
            except (xerror.ConnectionClosedError, OSError):
                pass

    def move(self, x: int, y: int) -> None:
        if self._x is None:
            return
        try:
            xtest.fake_input(self._x, X.MotionNotify, x=x + self.crop_x, y=y + self.crop_y)
            self._sync()
        except (xerror.ConnectionClosedError, OSError) as e:
            logger.debug("xtest move ignored ({})", e)

    def button(self, button: int, pressed: bool, x: int | None = None, y: int | None = None) -> None:
        if self._x is None:
            return
        try:
            if x is not None and y is not None:
                xtest.fake_input(self._x, X.MotionNotify, x=x + self.crop_x, y=y + self.crop_y)
            xtest.fake_input(self._x, X.ButtonPress if pressed else X.ButtonRelease, button)
            self._sync()
        except (xerror.ConnectionClosedError, OSError) as e:
            logger.debug("xtest button ignored ({})", e)

    def scroll(self, dx: float, dy: float) -> None:
        """Wheel = button 4 (up) / 5 (down) / 6 (left) / 7 (right), one press+release
        per notch. Browser deltaY>0 means scroll down."""
        if self._x is None:
            return
        try:
            for delta, up_btn, down_btn in ((dy, 4, 5), (dx, 6, 7)):
                if not delta:
                    continue
                button = down_btn if delta > 0 else up_btn
                for _ in range(min(10, max(1, int(abs(delta) / 40) or 1))):
                    xtest.fake_input(self._x, X.ButtonPress, button)
                    xtest.fake_input(self._x, X.ButtonRelease, button)
            self._sync()
        except (xerror.ConnectionClosedError, OSError) as e:
            logger.debug("xtest scroll ignored ({})", e)

    def key(self, code: str, key: str, pressed: bool) -> None:
        if self._x is None:
            return
        keysym = _keysym_for(code, key)
        if not keysym:
            return
        try:
            keycode = self._keycode_for(keysym)
            if keycode:
                xtest.fake_input(self._x, X.KeyPress if pressed else X.KeyRelease, keycode)
                self._sync()
        except (xerror.ConnectionClosedError, OSError) as e:
            logger.debug("xtest key ignored ({})", e)

    def _keycode_for(self, keysym: int) -> int:
        """Keycode for a keysym, binding it to a spare keycode if the base keymap
        lacks it (so any character types, and its keyup uses the same keycode)."""
        assert self._x is not None
        if keysym in self._scratch:
            return self._scratch[keysym]
        keycode = self._x.keysym_to_keycode(keysym)
        if keycode:
            return keycode
        if self._spare_keycode is None:
            return 0
        # Bind keysym onto the spare keycode (both shift levels), then use it.
        self._x.change_keyboard_mapping(self._spare_keycode, [[keysym, keysym]])
        self._x.sync()
        self._scratch[keysym] = self._spare_keycode
        return self._spare_keycode

    async def close(self) -> None:
        if self._x is not None:
            try:
                self._x.close()
            except (xerror.ConnectionClosedError, OSError):
                pass
            self._x = None
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (ProcessLookupError, TimeoutError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
        _free_display_num(self.num)
