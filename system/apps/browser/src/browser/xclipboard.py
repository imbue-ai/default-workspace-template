"""Bidirectional clipboard bridge for the streamed browser (text + images).

Copy-OUT (remote browser -> viewer) is event-driven with zero idle cost: a
python-xlib XFixes selection-owner monitor on the session display fires only
when the remote CLIPBOARD actually changes (no polling), reads the selection
with ``xclip``, and hands it to a callback. Paste-IN (viewer -> remote) sets
the X selection with ``xclip`` and lets the caller inject Ctrl+V.

xclip does the ICCCM selection dance (TARGETS negotiation, INCR for big
payloads) so we don't reimplement it; it is already installed in the workspace
image. python-xlib (already a dependency, see xinput) provides only the XFixes
change notification -- the one thing xclip can't give us without polling.

Echo suppression: after we set the clipboard ourselves (a paste-in), the
monitor's own change event would otherwise bounce our bytes straight back to
the viewer. We record the last bytes we wrote and skip a read that matches.
"""

import contextlib
import hashlib
import os
import select
import subprocess
import threading

import Xlib.display
import Xlib.error
from loguru import logger
from Xlib.ext import xfixes

# Image mimes we try first (a remote copy of a picture), then text.
_IMAGE_TARGETS = ("image/png", "image/jpeg", "image/bmp")
# Never read a selection larger than this from the remote (memory / transport
# guard; matches the paste-in cap and Selkies' file-clip ceiling).
_READ_MAX_BYTES = 10 * 1024 * 1024


class ClipboardError(RuntimeError):
    pass


def _xclip_base(display: str) -> list[str]:
    # -display as a flag (not the DISPLAY env): matches the proven browser-app
    # invocation and is robust regardless of the service's own environment.
    return ["xclip", "-display", display, "-selection", "clipboard"]


def set_clipboard(display: str, data: bytes, mime: str) -> None:
    """Own the remote CLIPBOARD with ``data`` of ``mime`` (text or image).

    ``xclip -i`` forks a BACKGROUND process that keeps serving the selection
    until another app claims it -- so stdout/stderr MUST be DEVNULL, never
    pipes: a captured pipe is inherited by that persistent child and keeps the
    parent's communicate() blocked forever (the bug this replaced).
    """
    args = _xclip_base(display)
    if not mime.startswith("text/"):
        args += ["-t", mime]
    args += ["-i"]
    result = subprocess.run(
        args, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
    )
    if result.returncode != 0:
        raise ClipboardError(f"xclip set exited {result.returncode}")


def _xclip_out(display: str, target: str) -> bytes | None:
    """One ``xclip -o -t <target>`` read; None on failure (xclip -o exits, so
    capturing its stdout is safe -- unlike the -i write above)."""
    result = subprocess.run(
        [*_xclip_base(display), "-o", "-t", target],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
    )
    return result.stdout if result.returncode == 0 else None


def read_clipboard(display: str) -> tuple[bytes, str] | None:
    """Read the current remote selection, preferring an image over text.

    Returns (bytes, mime) or None when the clipboard is empty/unreadable or
    over the size cap.
    """
    targets = _xclip_out(display, "TARGETS")
    available = set(targets.decode(errors="replace").split()) if targets else set()
    for mime in _IMAGE_TARGETS:
        if mime in available:
            data = _xclip_out(display, mime)
            if data and 0 < len(data) <= _READ_MAX_BYTES:
                return data, mime
    data = _xclip_out(display, "UTF8_STRING")
    if data and 0 < len(data) <= _READ_MAX_BYTES:
        return data, "text/plain"
    return None


class ClipboardMonitor:
    """XFixes CLIPBOARD-ownership watcher on one display; event-driven, no poll.

    The X connection is not thread-safe, so all X calls happen on this monitor's
    own thread; ``select`` on the X fd plus a self-pipe lets ``close`` wake it.
    """

    def __init__(self, display: str, on_change) -> None:  # noqa: ANN001  (callback)
        self.display = display
        self._display_name = display
        self._on_change = on_change
        self._last_written_hash: str | None = None
        self._stopping = threading.Event()
        self._stop_r, self._stop_w = os.pipe()
        self._thread = threading.Thread(target=self._run, name="clipboard-monitor", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def note_written(self, data: bytes) -> None:
        """Record bytes we just set, so the resulting change event is skipped."""
        self._last_written_hash = hashlib.sha1(data).hexdigest()

    def _run(self) -> None:
        try:
            disp = Xlib.display.Display(self._display_name)
        except Exception as error:  # noqa: BLE001  (Xlib raises assorted connection errors)
            logger.warning("clipboard monitor could not open {} ({})", self._display_name, error)
            return
        try:
            disp.xfixes_query_version()
            clipboard_atom = disp.intern_atom("CLIPBOARD")
            root = disp.screen().root
            disp.xfixes_select_selection_input(root, clipboard_atom, xfixes.XFixesSetSelectionOwnerNotifyMask)
            disp.flush()
            x_fd = disp.fileno()
            while not self._stopping.is_set():
                readable, _, _ = select.select([x_fd, self._stop_r], [], [])
                if self._stop_r in readable:
                    return
                for _ in range(disp.pending_events()):
                    disp.next_event()  # drain; we only care that ownership changed
                self._handle_change()
        except Exception as error:  # noqa: BLE001  (a monitor thread crash must not be silent)
            logger.warning("clipboard monitor loop ended ({})", error)
        finally:
            # close() flushes, which re-raises a connection that the server already
            # dropped (Xvfb/browser teardown) -- an Xlib.error.ConnectionClosedError, which
            # is NOT an OSError, so it must be suppressed explicitly or it escapes this
            # finally and kills the thread with a traceback on every browser shutdown.
            with contextlib.suppress(OSError, Xlib.error.ConnectionClosedError):
                disp.close()

    def _handle_change(self) -> None:
        payload = read_clipboard(self._display_name)
        if payload is None:
            return
        data, mime = payload
        if hashlib.sha1(data).hexdigest() == self._last_written_hash:
            return  # our own paste-in bouncing back
        try:
            self._on_change(data, mime)
        except Exception as error:  # noqa: BLE001  (a bad callback must not kill the monitor)
            logger.warning("clipboard on_change callback failed ({})", error)

    def close(self) -> None:
        self._stopping.set()
        with contextlib.suppress(OSError):
            os.write(self._stop_w, b"x")
        self._thread.join(timeout=3)
        for fd in (self._stop_r, self._stop_w):
            with contextlib.suppress(OSError):
                os.close(fd)
