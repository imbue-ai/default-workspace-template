"""The X clipboard on the Screen's display (docs/system/specs/screen/spec.md section 13, D14), the browser app's
bridge carried over: ``xclip`` sets and reads the CLIPBOARD selection (it does the ICCCM dance, TARGETS and INCR
included), and a python-xlib XFixes watcher notices an ownership change without polling, so the service can tell
viewers when the remote clipboard holds an image. Echo suppression: bytes the service itself placed are remembered
by hash and the change they cause is skipped."""

import contextlib
import hashlib
import os
import select
import subprocess
import threading
from collections.abc import Callable
from typing import Final

import Xlib.display
import Xlib.error
from Xlib.ext import xfixes
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from screen.errors import ClipboardError
from screen.primitives import DISPLAY

# The image targets a remote copy of a picture offers, tried before text.
IMAGE_TARGETS: Final[tuple[str, ...]] = ("image/png", "image/jpeg", "image/bmp")
TEXT_TARGET: Final[str] = "UTF8_STRING"
# Never read or place more than this (the viewer's cap too).
MAX_CLIPBOARD_BYTES: Final[int] = 10 * 1024 * 1024
_XCLIP_TIMEOUT_SECONDS: Final[float] = 5.0
_MONITOR_JOIN_SECONDS: Final[float] = 3.0


class ClipboardContents(FrozenModel):
    """What the remote clipboard holds right now, as far as the service reads it."""

    targets: tuple[str, ...] = Field(description="Every target the owner advertises")

    @property
    def image_target(self) -> str | None:
        return next((target for target in IMAGE_TARGETS if target in self.targets), None)

    @property
    def has_image(self) -> bool:
        return self.image_target is not None


@pure
def parse_targets(output: bytes | None) -> ClipboardContents:
    """The TARGETS ``xclip -o`` printed, one per line."""
    if output is None:
        return ClipboardContents(targets=())
    return ClipboardContents(targets=tuple(output.decode(errors="replace").split()))


@pure
def content_hash(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class XClipboard(MutableModel):
    """``xclip`` on one display."""

    display: str = Field(default=DISPLAY, frozen=True, description="The X display")
    xclip_command: tuple[str, ...] = Field(default=("xclip",), frozen=True, description="How xclip is invoked")

    def _base(self) -> list[str]:
        # ``-display`` as a flag rather than the environment: the service's own environment has no display.
        return [*self.xclip_command, "-display", self.display, "-selection", "clipboard"]

    def set_contents(self, data: bytes, mime: str) -> None:
        """Own the CLIPBOARD with ``data``. ``xclip -i`` forks a child that keeps serving the selection until another
        owner claims it, so its stdout and stderr go to the null device: a pipe inherited by that child would keep
        this call blocked for as long as the selection lives."""
        if len(data) > MAX_CLIPBOARD_BYTES:
            raise ClipboardError(f"the clipboard payload is {len(data)} bytes, past the {MAX_CLIPBOARD_BYTES} cap")
        arguments = self._base()
        if not mime.startswith("text/"):
            arguments += ["-t", mime]
        arguments += ["-i"]
        try:
            completed = subprocess.run(
                arguments,
                input=data,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_XCLIP_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ClipboardError(f"xclip could not set the clipboard: {e}") from e
        if completed.returncode != 0:
            raise ClipboardError(f"xclip -i exited {completed.returncode}")

    def _read_target(self, target: str) -> bytes | None:
        try:
            completed = subprocess.run(
                [*self._base(), "-o", "-t", target],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=_XCLIP_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.debug("xclip could not read {}: {}", target, e)
            return None
        return completed.stdout if completed.returncode == 0 else None

    def contents(self) -> ClipboardContents:
        return parse_targets(self._read_target("TARGETS"))

    def read_image(self) -> tuple[bytes, str] | None:
        """The image the clipboard holds, as (bytes, mime), or None when it holds none or it is over the cap."""
        found = self.contents()
        if found.image_target is None:
            return None
        data = self._read_target(found.image_target)
        if data is None or not 0 < len(data) <= MAX_CLIPBOARD_BYTES:
            return None
        return data, found.image_target


ClipboardChangeHandler = Callable[[ClipboardContents], None]


class ClipboardMonitor(MutableModel):
    """An XFixes watcher of CLIPBOARD ownership on one display, event-driven. The X connection is not thread-safe,
    so every X call happens on the monitor's own thread; ``select`` over the X socket and a self-pipe lets ``stop``
    wake it."""

    model_config = {"arbitrary_types_allowed": True}

    clipboard: XClipboard = Field(frozen=True, description="Reads the targets after a change")
    on_change: ClipboardChangeHandler = Field(frozen=True, description="Called with the contents after each change")

    _stopping: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)
    _wake_read_fd: int = PrivateAttr(default=-1)
    _wake_write_fd: int = PrivateAttr(default=-1)
    _last_written_hash: str | None = PrivateAttr(default=None)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._wake_read_fd, self._wake_write_fd = os.pipe()
        self._thread = threading.Thread(target=self._run, name="screen-clipboard-monitor", daemon=True)
        self._thread.start()

    def note_written(self, data: bytes) -> None:
        """Remember bytes the service just placed, so the change they cause is not reported back."""
        self._last_written_hash = content_hash(data)

    def _run(self) -> None:
        try:
            display = Xlib.display.Display(self.clipboard.display)
        except (Xlib.error.DisplayError, OSError) as e:
            logger.warning("The clipboard monitor could not open {} ({})", self.clipboard.display, e)
            return
        try:
            display.xfixes_query_version()
            clipboard_atom = display.intern_atom("CLIPBOARD")
            display.xfixes_select_selection_input(
                display.screen().root, clipboard_atom, xfixes.XFixesSetSelectionOwnerNotifyMask
            )
            display.flush()
            x_fd = display.fileno()
            while not self._stopping.is_set():
                readable, _, _ = select.select([x_fd, self._wake_read_fd], [], [])
                if self._wake_read_fd in readable:
                    return
                for _ in range(display.pending_events()):
                    display.next_event()
                self._handle_change()
        except (Xlib.error.XError, Xlib.error.DisplayError, Xlib.error.ConnectionClosedError, OSError) as e:
            logger.warning("The clipboard monitor stopped ({})", e)
        finally:
            # Closing flushes, which raises on a connection the server already dropped (the stack ending).
            with contextlib.suppress(OSError, Xlib.error.ConnectionClosedError):
                display.close()

    def _handle_change(self) -> None:
        found = self.clipboard.contents()
        if found.has_image and self._last_written_hash is not None:
            image = self.clipboard.read_image()
            if image is not None and content_hash(image[0]) == self._last_written_hash:
                return
        self.on_change(found)

    def stop(self) -> None:
        self._stopping.set()
        if self._wake_write_fd >= 0:
            with contextlib.suppress(OSError):
                os.write(self._wake_write_fd, b"x")
        if self._thread is not None:
            self._thread.join(timeout=_MONITOR_JOIN_SECONDS)
        for fd in (self._wake_read_fd, self._wake_write_fd):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._wake_read_fd = self._wake_write_fd = -1
