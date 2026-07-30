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
from loguru import logger
from Xlib.ext import xfixes

# Image mimes we try first (a remote copy of a picture), then text.
_IMAGE_TARGETS = ("image/png", "image/jpeg", "image/bmp")
# Never read a selection larger than this from the remote (memory / transport
# guard; matches the paste-in cap and Selkies' file-clip ceiling).
_READ_MAX_BYTES = 10 * 1024 * 1024


class ClipboardError(RuntimeError):
    pass


def _xclip(args: list[str], display: str, data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["xclip", "-selection", "clipboard", *args],
        env={**os.environ, "DISPLAY": display},
        input=data,
        capture_output=True,
        timeout=10,
    )


def set_clipboard(display: str, data: bytes, mime: str) -> None:
    """Own the remote CLIPBOARD with ``data`` of ``mime`` (text or image)."""
    args = ["-i"] if mime.startswith("text/") else ["-t", mime, "-i"]
    result = _xclip(args, display, data)
    if result.returncode != 0:
        raise ClipboardError(f"xclip set failed: {result.stderr.decode(errors='replace')[:200]}")


def read_clipboard(display: str) -> tuple[bytes, str] | None:
    """Read the current remote selection, preferring an image over text.

    Returns (bytes, mime) or None when the clipboard is empty/unreadable or
    over the size cap.
    """
    targets = _xclip(["-o", "-t", "TARGETS"], display)
    available = set(targets.stdout.decode(errors="replace").split()) if targets.returncode == 0 else set()
    for mime in _IMAGE_TARGETS:
        if mime in available:
            out = _xclip(["-o", "-t", mime], display)
            if out.returncode == 0 and 0 < len(out.stdout) <= _READ_MAX_BYTES:
                return out.stdout, mime
    out = _xclip(["-o"], display)
    if out.returncode == 0 and 0 < len(out.stdout) <= _READ_MAX_BYTES:
        return out.stdout, "text/plain"
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
            with contextlib.suppress(OSError):
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
