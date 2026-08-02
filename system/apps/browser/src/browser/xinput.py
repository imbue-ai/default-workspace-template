"""X11 input injection for the streamed browser, ported from Selkies.

The ``_XTestKeyboard`` and ``_XTestMouse`` classes are copied (lightly trimmed)
from the Selkies project's ``src/selkies/input_handler.py``
(https://github.com/selkies-project/selkies, MPL-2.0); this file is therefore
subject to the Mozilla Public License, v. 2.0. If a copy of the MPL was not
distributed with this file, you can obtain one at https://mozilla.org/MPL/2.0/.
They are the canonical solved version of display-level key injection: keysym ->
keycode resolution across Shift/AltGr levels, dynamic overlay keycodes for
keysyms the layout lacks (Unicode), press-time keycode replay on release, and
synthesized-modifier bookkeeping.

``InputRouter`` is ours: a lean server-side parser for the Selkies client
message grammar (comma-separated text frames on the stream socket):

    kd,<keysym>                     key down
    ku,<keysym>                     key up
    kr                              release everything (focus loss / disconnect)
    m,<x>,<y>,<button_mask>,<mag>   absolute pointer state; mask bits
                                    0=left 1=middle 2=right 3=scroll-up
                                    4=scroll-down, diffed against the previous
                                    mask; scroll bits inject <mag> wheel clicks

Deliberately omitted from the canonical implementation (v1 scope): clipboard
sync, gamepads, touch, relative pointer mode, key auto-repeat arming, and the
compositor-atomic typing path (X11-only here; the overlay keycodes cover
unmapped keysyms instead).
"""

import struct
import threading
import time

import Xlib.error
import Xlib.X
import Xlib.Xatom
from loguru import logger
from Xlib.display import Display
from Xlib.ext import xtest

_SCROLL_MAX_CLICKS = 64


class _XTestKeyboard:
    """Keyboard controller backed by python-xlib's XTEST extension (Selkies).

    Injects key events through the already-open xdisplay connection. Spare
    keycodes past the base layout are bound on demand to keysyms the layout
    lacks (Unicode, exotic symbols) so they inject in-process; round-robin
    recycled.
    """

    # Settle after rebinding a RECYCLED keycode: the server applies the mapping
    # synchronously (sync()), but xcb-class toolkits refetch keymaps
    # asynchronously and could translate the queued press with the old symbol.
    _RECYCLE_SETTLE_S = 0.01

    def __init__(self, xdisplay) -> None:  # noqa: ANN001  (Xlib display object)
        self._d = xdisplay
        # XK_Shift_L; may be 0 on an exotic keymap (then capitals just skip shift).
        self._shift_kc = xdisplay.keysym_to_keycode(0xFFE1)
        # AltGr, to reach glyphs bound above the Shift level: prefer
        # ISO_Level3_Shift, fall back to Mode_switch.
        self._altgr_kc = xdisplay.keysym_to_keycode(0xFE03) or xdisplay.keysym_to_keycode(0xFF7E)
        # keysym -> the modifier keycodes press() synthesized for it; release()
        # undoes only these.
        self._synth_mods: dict[int, list[int]] = {}
        self._spare_keycodes: list[int] | None = None
        self._overlay: dict[int, int] = {}  # keysym -> keycode
        self._overlay_order: list[int] = []  # round-robin recycle order
        self._pressed_kc: dict[int, int] = {}  # keysym -> keycode injected at press

    def _find_spare_keycodes(self) -> list[int]:
        info = self._d.display.info
        lo, hi = info.min_keycode, info.max_keycode
        mapping = self._d.get_keyboard_mapping(lo, hi - lo + 1)
        return [lo + i for i, syms in enumerate(mapping) if all(s == 0 for s in syms)]

    def _overlay_keycode(self, keysym: int) -> int | None:
        """Bind an unmapped keysym to a spare keycode (recycling the oldest)."""
        if keysym in self._overlay:
            return self._overlay[keysym]
        if self._spare_keycodes is None:
            self._spare_keycodes = self._find_spare_keycodes()
        if not self._spare_keycodes:
            return None
        used = set(self._overlay.values())
        free = [kc for kc in self._spare_keycodes if kc not in used]
        if free:
            keycode = free[0]
            recycled = False
        else:
            oldest = self._overlay_order.pop(0)
            keycode = self._overlay.pop(oldest)
            recycled = True
        self._overlay[keysym] = keycode
        self._overlay_order.append(keysym)
        # Assign at levels 0 and 1 so an accidental Shift can't change it.
        self._d.change_keyboard_mapping(keycode, [[keysym, keysym]])
        self._d.sync()
        if recycled:
            time.sleep(self._RECYCLE_SETTLE_S)
        return keycode

    def _resolve(self, keysym: int) -> tuple[int, tuple[int, ...]]:
        """(keycode, modifier keycodes selecting the keymap level the keysym sits at)."""
        d = self._d
        keycode = d.keysym_to_keycode(keysym)
        if not keycode:
            keycode = self._overlay_keycode(keysym)
            if not keycode:
                raise InputInjectionError(f"no keycode available for keysym {keysym!r}")
            return keycode, ()
        # Lowest column carrying this glyph: 0 base, 1 Shift, 2 AltGr, 3 both.
        level = next((lvl for lvl in range(4) if d.keycode_to_keysym(keycode, lvl) == keysym), 0)
        mods = []
        if level & 1 and self._shift_kc:
            mods.append(self._shift_kc)
        if level & 2 and self._altgr_kc:
            mods.append(self._altgr_kc)
        return keycode, tuple(mods)

    def _mod_down(self, keycode: int) -> bool:
        bits = self._d.query_keymap()
        return bool(bits[keycode // 8] & (1 << (keycode % 8)))

    def press(self, keysym: int) -> None:
        keycode, mods = self._resolve(keysym)
        synth = [m for m in mods if not self._mod_down(m)]
        for m in synth:
            xtest.fake_input(self._d, Xlib.X.KeyPress, m)
        if synth:
            self._synth_mods[keysym] = synth
        xtest.fake_input(self._d, Xlib.X.KeyPress, keycode)
        self._pressed_kc[keysym] = keycode  # replay this exact keycode on release
        self._d.flush()

    def release(self, keysym: int) -> None:
        keycode = self._pressed_kc.pop(keysym, None)
        if keycode is None:
            keycode, _ = self._resolve(keysym)
        xtest.fake_input(self._d, Xlib.X.KeyRelease, keycode)
        for m in reversed(self._synth_mods.pop(keysym, [])):
            xtest.fake_input(self._d, Xlib.X.KeyRelease, m)
        self._d.flush()


class _XTestMouse:
    """Pointer controller backed by python-xlib's XTEST extension (Selkies)."""

    def __init__(self, xdisplay) -> None:  # noqa: ANN001  (Xlib display object)
        self._d = xdisplay

    def move(self, x: int, y: int) -> None:
        xtest.fake_input(self._d, Xlib.X.MotionNotify, detail=False, root=Xlib.X.NONE, x=int(x), y=int(y))
        self._d.flush()

    def button(self, button: int, down: bool) -> None:
        xtest.fake_input(self._d, Xlib.X.ButtonPress if down else Xlib.X.ButtonRelease, int(button))
        self._d.flush()

    def scroll_clicks(self, button: int, count: int) -> None:
        # X core scroll buttons: 4=up, 5=down.
        for _ in range(count):
            xtest.fake_input(self._d, Xlib.X.ButtonPress, button)
            xtest.fake_input(self._d, Xlib.X.ButtonRelease, button)
        self._d.flush()


class InputInjectionError(RuntimeError):
    pass


def diff_button_mask(previous: int, current: int, scroll_magnitude: int) -> list[tuple[str, int]]:
    """Actions for a Selkies button-mask transition, as (kind, arg) tuples.

    kind "button": arg is the X button number to toggle (the caller pairs this
    with the mask bit's press/release state); kind "scroll": arg is the X
    scroll button, fired ``scroll_magnitude`` times on the bit's rising edge.
    Pure so the mask semantics are unit-testable without a display.
    """
    actions: list[tuple[str, int]] = []
    magnitude = max(0, min(int(scroll_magnitude), _SCROLL_MAX_CLICKS))
    for bit, x_button in ((0, 1), (1, 2), (2, 3)):
        if (previous ^ current) & (1 << bit):
            actions.append(("button", x_button))
    for bit, x_button in ((3, 4), (4, 5)):
        rising = current & (1 << bit) and not previous & (1 << bit)
        if rising and magnitude:
            actions.append(("scroll", x_button))
    return actions


class InputRouter:
    """Parse client input messages and inject them into one X display.

    One router per stream connection; ``close()`` releases everything still
    held so a dropped viewer can never leave a stuck key or button behind
    (the same guarantee Selkies' pressed-key tracking provides).
    """

    def __init__(self, display_name: str) -> None:
        self._display = Display(display_name)
        self._keyboard = _XTestKeyboard(self._display)
        self._mouse = _XTestMouse(self._display)
        self._pressed_keysyms: set[int] = set()
        self._button_mask = 0
        self._lock = threading.Lock()
        # Identify the browser window by TYPE, never size (a default-sized Ctrl+N window
        # would fool a size heuristic). Matches the window guardian's discriminator.
        self._window_type_atom = self._display.intern_atom("_NET_WM_WINDOW_TYPE")
        self._normal_type_atom = self._display.intern_atom("_NET_WM_WINDOW_TYPE_NORMAL")

    def handle(self, message: str) -> None:
        """Dispatch one client text frame; malformed input logs and drops
        (never tears down the stream -- Selkies' contract)."""
        try:
            self._dispatch(message)
        except (ValueError, IndexError, InputInjectionError, struct.error, Xlib.error.XError) as error:
            # struct.error/XError guard a hostile frame whose out-of-range values would
            # otherwise escape python-xlib's serialization and tear down the receive thread.
            logger.warning("dropped malformed/uninjectable input {!r} ({})", message[:64], error)

    def _dispatch(self, message: str) -> None:
        tokens = message.split(",")
        kind = tokens[0]
        if kind == "kd":
            keysym = int(tokens[1])
            if not 0 <= keysym <= 0xFFFFFFFF:
                return  # out of Card32 range: drop (would raise struct.error in XTEST/change_keyboard_mapping)
            with self._lock:
                self._pressed_keysyms.add(keysym)
                self._keyboard.press(keysym)
        elif kind == "ku":
            keysym = int(tokens[1])
            if not 0 <= keysym <= 0xFFFFFFFF:
                return
            with self._lock:
                self._pressed_keysyms.discard(keysym)
                self._keyboard.release(keysym)
        elif kind == "kr":
            self.release_all()
        elif kind == "kh":
            # Held-key heartbeat (Selkies): the client periodically reports the
            # full set it believes is held. Anything we track that the client
            # no longer holds lost its keyup in transit -- release it, so a
            # dropped message can never leave a key stuck down.
            reported = {int(token) for token in tokens[1:] if token}
            with self._lock:
                for keysym in list(self._pressed_keysyms - reported):
                    self._pressed_keysyms.discard(keysym)
                    # Broad on purpose: the sweep must visit every stale key.
                    try:
                        self._keyboard.release(keysym)
                    except Exception:  # noqa: BLE001
                        logger.debug("failed releasing stale keysym {}", keysym)
        elif kind == "m":
            x, y, mask, magnitude = (int(t) for t in tokens[1:5])
            # Clamp coords to int16 (the XTEST wire type); an out-of-range coordinate would
            # otherwise raise struct.error and tear down the stream.
            x = max(-32768, min(32767, x))
            y = max(-32768, min(32767, y))
            with self._lock:
                self._mouse.move(x, y)
                for action, x_button in diff_button_mask(self._button_mask, mask, magnitude):
                    if action == "button":
                        self._mouse.button(x_button, bool(mask & (1 << (x_button - 1))))
                    else:
                        self._mouse.scroll_clicks(x_button, max(1, min(magnitude, _SCROLL_MAX_CLICKS)))
                self._button_mask = mask & 0b111  # scroll bits are edges, not state
        else:
            logger.debug("ignoring unknown input message kind {!r}", kind)

    def release_all(self) -> None:
        """Release every held key and button (focus loss, disconnect)."""
        with self._lock:
            for keysym in list(self._pressed_keysyms):
                # Broad on purpose: the reset must visit every held key even if
                # one release explodes (Xlib raises assorted protocol errors).
                try:
                    self._keyboard.release(keysym)
                except Exception:  # noqa: BLE001
                    logger.debug("failed releasing keysym {} during reset", keysym)
            self._pressed_keysyms.clear()
            for bit, x_button in ((0, 1), (1, 2), (2, 3)):
                if self._button_mask & (1 << bit):
                    self._mouse.button(x_button, False)
            self._button_mask = 0

    def paste(self) -> None:
        """Inject Ctrl+V so the focused page pastes the current X selection.
        Used by the paste-in HTTP path after the selection is set."""
        with self._lock:
            try:
                self._keyboard.press(0xFFE3)   # Control_L
                self._keyboard.press(0x0076)   # v
                self._keyboard.release(0x0076)
                self._keyboard.release(0xFFE3)
            except Exception:  # noqa: BLE001  (injection best-effort; never raise to the route)
                logger.debug("paste injection failed")

    def resize_window(self, width: int, height: int) -> None:
        """Resize the browser's toplevel window to fill the pane (no WM here, so we configure
        the window directly), anchored at the origin so capture-region coords map 1:1. The
        window is found by TYPE and id (the lowest-id NORMAL top-level -- the same window the
        guardian keeps), NOT by size: a default-sized Ctrl+N window would fool a size pick. X
        errors (a window that vanished mid-call) are swallowed -- a failed resize is cosmetic."""
        with self._lock:
            # Broad on purpose: python-xlib raises assorted protocol errors
            # (BadWindow/BadMatch) if the window changes under us; none should
            # take the input thread down.
            try:
                main = self._main_browser_window()
                if main is not None:
                    main.configure(x=0, y=0, width=width, height=height)
                    self._display.sync()
            except Exception:  # noqa: BLE001
                logger.debug("resize_window({},{}) failed (window changed?)", width, height)

    def _main_browser_window(self):  # noqa: ANN202  (Xlib window object)
        """The browser's main top-level: the lowest-id viewable, non-override window of type
        NORMAL. Identified by TYPE + id (never size), matching the window guardian's choice so
        input and resize target the same window the guardian keeps."""
        root = self._display.screen().root
        main = None
        for window in root.query_tree().children:
            attrs = window.get_attributes()
            if attrs.map_state != Xlib.X.IsViewable or attrs.override_redirect:
                continue
            prop = window.get_full_property(self._window_type_atom, Xlib.Xatom.ATOM)
            if prop is not None and prop.value and self._normal_type_atom not in prop.value:
                continue  # a dialog/utility popup, not a browser window
            if main is None or window.id < main.id:
                main = window
        return main

    def close(self) -> None:
        self.release_all()
        try:
            self._display.close()
        except Xlib.error.ConnectionClosedError:
            # The browser's X server is already gone (e.g. teardown on daemon restart):
            # closing our end best-effort. Swallow so it doesn't escape the /stream finally.
            logger.debug("input router display already closed")
