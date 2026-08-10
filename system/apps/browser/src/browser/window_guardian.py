"""Single-window guardian: keep each browser to exactly ONE top-level window, pinned to the
capture region.

The fleet captures Chrome's own window off a bare Xvfb with NO window manager, so two things
break the model: Ctrl+N (or dragging a tab out) opens a SECOND top-level window the capture
can't show coherently, and -- with no WM to constrain it -- the window can be dragged off
(0,0), sliding out of the (0,0,w,h) capture region. This small per-viewer thread, on its own
X connection, re-pins the main window to the pane every tick and closes any extra browser
window (raising a one-time signal so the client can explain why), holding the one-window
invariant the rest of the pipeline assumes.

A second browser window is identified by _NET_WM_WINDOW_TYPE == NORMAL, not by size: a Ctrl+N
window opens at Chromium's default 1280x800 while the main is pinned to the (often larger)
pane, so a size heuristic misclassifies it -- but every real browser window is type NORMAL and
Chrome's own popups (print/save dialogs, bubbles) are DIALOG/UTILITY/etc., so type is the clean
discriminator. Of the NORMAL windows the guardian keeps the lowest X id -- the oldest, i.e. the
user's original -- and closes the rest; picking deterministically means several viewers' guardians
all agree on the same main and never fight over it.
"""

import contextlib
import threading
from typing import Any

import Xlib.error
from loguru import logger
from Xlib import X, Xatom, protocol
from Xlib.display import Display

# One check a second: cheap (a query_tree + a few property reads), snaps a dragged window back
# within a second and closes a stray window promptly without busy-spinning the X connection.
_GUARD_INTERVAL_S = 1.0

# "A stray window was just closed" is a BROWSER-wide fact, but several viewers of one browser
# each run their own guardian -- whichever ticks first closes the window, so a per-guardian
# flag would land the modal on the wrong (or a paused) viewer. Signal browser-wide instead and
# let the one ACTIVE viewer's sender loop drain it, so the modal always reaches who's watching.
_closed_lock = threading.Lock()
_closed_signals: "dict[str, bool]" = {}


def signal_extra_closed(browser_id: str) -> None:
    with _closed_lock:
        _closed_signals[browser_id] = True


def take_extra_closed(browser_id: str) -> bool:
    """True (once) if a stray window was closed for this browser since the last call. Pops the
    key so the map holds only pending signals -- it can't accumulate one entry per browser id
    ever seen for the life of the process."""
    with _closed_lock:
        return bool(_closed_signals.pop(browser_id, False))


class WindowGuardian(threading.Thread):
    """Per-viewer thread that pins the browser to one window at the capture geometry."""

    def __init__(self, browser_id: str, display: str, stop_event: threading.Event) -> None:
        super().__init__(daemon=True)
        self._browser_id = browser_id
        self._display_name = display
        self._stop_event = stop_event  # NOT self._stop -- that shadows threading.Thread._stop and breaks join()
        self._seen_extra: "set[int]" = set()  # extra window ids already actioned (one signal each)

    def run(self) -> None:
        try:
            disp = Display(self._display_name)
        except Exception as error:  # noqa: BLE001  (a display that won't open just disables the guard)
            logger.warning("window guardian could not open display {} ({})", self._display_name, error)
            return
        atoms = {
            "wm_protocols": disp.intern_atom("WM_PROTOCOLS"),
            "wm_delete": disp.intern_atom("WM_DELETE_WINDOW"),
            "window_type": disp.intern_atom("_NET_WM_WINDOW_TYPE"),
            "type_normal": disp.intern_atom("_NET_WM_WINDOW_TYPE_NORMAL"),
        }
        root = disp.screen().root
        try:
            while not self._stop_event.wait(_GUARD_INTERVAL_S):
                try:
                    self._tick(disp, root, atoms)
                except Xlib.error.ConnectionClosedError:
                    return  # the browser's X server went away (teardown) -- stop quietly
                except Exception as error:  # noqa: BLE001  (a transient X error must not kill the guard)
                    logger.debug("window guardian tick error ({})", error)
        finally:
            with contextlib.suppress(Exception):
                disp.close()

    def _tick(self, disp: Any, root: Any, atoms: dict) -> None:
        windows = []  # (id, window, geometry) for each real browser (type NORMAL) top-level
        for window in root.query_tree().children:
            try:
                attrs = window.get_attributes()
                if attrs.map_state != X.IsViewable or attrs.override_redirect:
                    continue
                if not self._is_browser_window(window, atoms):
                    continue  # a dialog/utility popup, not a browser window -- leave it (by TYPE, never size)
                geometry = window.get_geometry()
                windows.append((window.id, window, geometry))
            except Xlib.error.BadWindow:
                continue  # window vanished between query_tree and the read
        if not windows:
            return
        windows.sort(key=lambda entry: entry[0])  # by X id ascending -> lowest = oldest = the main
        _main_id, main_window, main_geometry = windows[0]
        self._repin(main_window, main_geometry)
        for window_id, window, _geometry in windows[1:]:
            self._close(window, atoms)
            if window_id not in self._seen_extra:
                self._seen_extra.add(window_id)
                signal_extra_closed(self._browser_id)  # first sight of this stray -> one client modal
        self._seen_extra &= {entry[0] for entry in windows[1:]}  # forget ids now gone, so a reuse re-signals
        disp.sync()

    @staticmethod
    def _is_browser_window(window: Any, atoms: dict) -> bool:
        """A real browser window is _NET_WM_WINDOW_TYPE == NORMAL (or has no type set); Chrome's
        own popups are DIALOG/UTILITY/etc. and must be left alone."""
        prop = window.get_full_property(atoms["window_type"], Xatom.ATOM)
        if prop is None or not prop.value:
            return True  # untyped top-level -> treat as a normal window
        return atoms["type_normal"] in prop.value

    @staticmethod
    def _repin(window: Any, geometry: Any) -> None:
        """Snap the window back to the top-left. POSITION only -- the window SIZE is owned by
        the active viewer's resize path (set_capture_region -> resize_window); if the guardian
        also set size, several viewers' guardians would each force their own pane size and fight
        over it. Position is always (0,0), so every guardian agrees and there's nothing to fight."""
        if (geometry.x, geometry.y) == (0, 0):
            return  # already home -- no needless reconfigure
        with contextlib.suppress(Xlib.error.BadWindow):
            window.configure(x=0, y=0)

    @staticmethod
    def _close(window: Any, atoms: dict) -> None:
        """Ask Chromium to close this window gracefully (WM_DELETE_WINDOW), which shuts just
        this window -- never XKillClient, which would drop the whole browser's X connection."""
        event = protocol.event.ClientMessage(
            window=window, client_type=atoms["wm_protocols"], data=(32, [atoms["wm_delete"], X.CurrentTime, 0, 0, 0])
        )
        with contextlib.suppress(Exception):
            window.send_event(event)
