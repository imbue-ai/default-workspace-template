"""Keep the X input focus on the browser window, on a display with no WM.

Without a window manager X stays in ``PointerRoot`` focus mode: pointer and
keyboard events reach whatever window is under the cursor, but no window ever
receives a ``FocusIn``. Chromium therefore believes it is unfocused, and
``document.hasFocus()`` is false in every page it renders. That is not cosmetic
-- Blink checks focus FIRST in ``ClipboardPromise::ValidatePreconditions``,
before permissions, so every ``navigator.clipboard.*`` call is rejected with
"Document is not focused." In practice: a site's own "Copy" button does nothing.

A window manager would fix this as a side effect, but the ones that would do it
put a synchronous pointer grab on Button1 of every client window and replay the
click -- inserting a process wakeup into the critical path of every click, which
is exactly what we are trying to keep short. Chromium already contains the
no-WM path we need: ``X11Window::Activate`` tests whether the WM advertises
``_NET_ACTIVE_WINDOW`` and, finding no WM, calls ``SetInputFocus`` on itself and
marks the window focused. So all this has to do is what a WM would have: set the
focus once the window appears, and put it back when something takes it away.

Re-asserting is the part that matters. Because Chromium sees no WM, every popup,
``<select>`` dropdown and ``window.open`` calls ``SetInputFocus`` on itself; when
that window unmaps, the focus it held goes with it and nothing restores it. So
this watches the display for structure and focus changes rather than setting
focus once and hoping.

``RevertToPointerRoot`` (not ``RevertToParent``) is deliberate: when the focused
window vanishes before we observe it, X falls back to pointer-root -- the
behaviour we had before -- rather than to a parent that may itself be gone.
"""

import threading
from typing import Any

from loguru import logger
from Xlib import X, display as xdisplay, error as xerror

# Errors talking to a display that is resizing, wedged, or being torn down.
# ConnectionClosedError is NOT a subclass of DisplayError in python-xlib.
_XLIB_ERRORS = (
    xerror.DisplayError,
    xerror.ConnectionClosedError,
    xerror.XError,
    OSError,
    ConnectionError,
    ValueError,
    AttributeError,
)

# Chromium's toplevels carry this WM_CLASS. Resolved by class rather than by
# caching a window id because Chromium destroys and recreates toplevels (a new
# window, a tab torn out), and a cached id would go stale silently.
_CHROMIUM_WM_CLASSES = ("chromium", "chromium-browser", "tilion", "fortress")


class FocusKeeper:
    """Holds X input focus on one display's browser window."""

    def __init__(self, display_name: str) -> None:
        self.display_name = display_name
        self._display: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        """Open the display and watch it until :meth:`stop`.

        Runs on its own thread with its own X connection: python-xlib connections
        are not thread-safe, and this one blocks on ``next_event`` between
        changes, so it costs nothing while idle.
        """
        self._display = xdisplay.Display(self.display_name)
        root = self._display.screen().root
        # SubstructureNotify tells us when a window is mapped or unmapped (a
        # popup opening or closing); FocusChange tells us when focus moves off
        # the window we set it to.
        root.change_attributes(event_mask=X.SubstructureNotifyMask | X.FocusChangeMask)
        self._display.sync()
        self._thread = threading.Thread(
            target=self._watch, name=f"focus-keeper-{self.display_name}", daemon=True
        )
        self._thread.start()
        self._apply()

    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                # Blocks until X has something to say. A display torn down under
                # us surfaces here as a connection error and ends the loop.
                self._display.next_event()
                while self._display.pending_events():
                    self._display.next_event()
            except _XLIB_ERRORS as e:
                if not self._stop.is_set():
                    logger.debug("focus keeper {} ending ({})", self.display_name, e)
                return
            self._apply()

    def _apply(self) -> None:
        """Point the input focus at the browser's toplevel, if it isn't already."""
        try:
            window = self._find_browser_window()
            if window is None:
                return
            focused = self._display.get_input_focus().focus
            if getattr(focused, "id", None) == window.id:
                return
            self._display.set_input_focus(window, X.RevertToPointerRoot, X.CurrentTime)
            self._display.sync()
        except _XLIB_ERRORS as e:
            logger.debug("focus keeper {} could not set focus ({})", self.display_name, e)

    def _find_browser_window(self) -> Any:
        """The browser's mapped toplevel, resolved by WM_CLASS each time."""
        root = self._display.screen().root
        for window in root.query_tree().children:
            try:
                if window.get_attributes().map_state != X.IsViewable:
                    continue
                wm_class = window.get_wm_class()
            except _XLIB_ERRORS:
                continue  # window died between listing and asking
            if wm_class and any(
                candidate.lower() in _CHROMIUM_WM_CLASSES for candidate in wm_class
            ):
                return window
        return None

    def stop(self) -> None:
        """Stop watching and close the connection. Idempotent."""
        self._stop.set()
        display, self._display = self._display, None
        if display is not None:
            try:
                display.close()  # unblocks next_event in the watcher thread
            except _XLIB_ERRORS as e:
                logger.debug("focus keeper {} close ignored ({})", self.display_name, e)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)
