"""A visiting agent's commands and files: run a command and stream its output, read a file, write a file.

The operations behind ``POST /_agent/run``, ``/_agent/files/read`` and
``/_agent/files/write``. The request and answer shapes are owner-exec's own
(``imbue-ai/owner-exec``, ``internal/server/server.go``) without its
signatures: the gateway has already checked the caller's admission token, and
that token is the whole of the caller's authority.

A command runs as the user the gateway runs as, with the gateway's environment.
Paths are not confined to the repository: a caller that may run any command
could reach any file that way, so a path rule here would protect nothing.
"""

import base64
import codecs
import errno
import math
import os
import re
import selectors
import signal
import socket
import stat
import subprocess
import threading
import time
from collections.abc import Generator
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from uuid import uuid4

DEFAULT_COMMAND_TIMEOUT_SECONDS = 600.0
MAX_COMMAND_TIMEOUT_SECONDS = 3600.0
MIN_COMMAND_TIMEOUT_SECONDS = 1.0
# A command's words never exceed what one exec accepts, which is far below this.
MAX_RUN_BODY_BYTES = 4 * 1024 * 1024
# owner-exec's cap on a request, which is what bounds a file written through it.
MAX_WRITE_BODY_BYTES = 64 * 1024 * 1024
# The most a write can carry once base64 has grown it by a third, so a file that can be read can be written back.
MAX_READ_FILE_BYTES = 48 * 1024 * 1024
DEFAULT_FILE_MODE = 0o600
_FILE_MODE_PATTERN = re.compile(r"0*[0-7]{1,3}")
_READ_CHUNK_BYTES = 64 * 1024
# A line with no newline is cut here, so a command that never ends its line cannot grow one event without bound.
_MAX_LINE_CHARACTERS = 1024 * 1024
# How often the caller of a command that has closed its output and runs on is looked at.
_CALLER_CHECK_SECONDS = 0.2

STDOUT_EVENT = "stdout"
STDERR_EVENT = "stderr"
EXIT_EVENT = "exit"
# What owner-exec reports for a command a signal ended.
_SIGNALLED_EXIT_CODE = -1


class InvalidAgentRequestError(ValueError):
    """Raised when a request's body names something the gateway cannot act on; the message is the answer's ``error``."""


class WorkspaceFileError(Exception):
    """Raised when a file cannot be read or written; the message says why, and never quotes the file."""


class RunRequest:
    """A command to run: its words, the directory it runs in, and how long it may take."""

    def __init__(self, command: list[str], cwd: Path, timeout_seconds: float) -> None:
        self.command = command
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds


class ReadFileRequest:
    """A file to read: the path as the caller wrote it, and where that points."""

    def __init__(self, raw_path: str, path: Path) -> None:
        self.raw_path = raw_path
        self.path = path


class WriteFileRequest:
    """A file to write: the path as the caller wrote it, where that points, the bytes, and the permission bits."""

    def __init__(self, raw_path: str, path: Path, content: bytes, mode: int) -> None:
        self.raw_path = raw_path
        self.path = path
        self.content = content
        self.mode = mode


def resolve_workspace_path(repo_root: Path, raw_path: str) -> Path:
    """Where a request's path points: itself when absolute, else under the repository root, which '' names."""
    return Path(os.path.normpath(repo_root / raw_path))


def resolve_timeout_seconds(raw: object) -> float:
    """How long a command may run: the default when none is asked for, else what was asked, held to the allowed range."""
    if raw is None:
        return DEFAULT_COMMAND_TIMEOUT_SECONDS
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or (isinstance(raw, float) and math.isnan(raw)):
        raise InvalidAgentRequestError("timeout_seconds must be a number of seconds")
    # Held to the range before the conversion, which an integer of any size survives only once it is in range.
    return float(min(max(raw, MIN_COMMAND_TIMEOUT_SECONDS), MAX_COMMAND_TIMEOUT_SECONDS))


def parse_file_mode(raw: object) -> int:
    """The permission bits a write asks for, from octal digits; owner-only read and write when it asks for none."""
    if raw is None or raw == "":
        return DEFAULT_FILE_MODE
    if not isinstance(raw, str) or _FILE_MODE_PATTERN.fullmatch(raw) is None:
        raise InvalidAgentRequestError("mode must be the octal digits of permission bits, like 644")
    return int(raw, 8)


def _command_words(raw: object) -> list[str]:
    if isinstance(raw, list) and raw:
        words = [word for word in raw if isinstance(word, str)]
        if len(words) == len(raw):
            return words
    raise InvalidAgentRequestError("command must be a non-empty list of strings")


def _is_file_path(raw_path: str) -> bool:
    """Whether the operating system takes ``raw_path`` as a path at all."""
    try:
        return b"\x00" not in os.fsencode(raw_path)
    except UnicodeEncodeError:
        return False


def _required_path(body: Mapping[str, object]) -> str:
    raw_path = body.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise InvalidAgentRequestError("path must be a non-empty string")
    if not _is_file_path(raw_path):
        raise InvalidAgentRequestError("path is not one a file can have")
    return raw_path


def parse_run_request(body: Mapping[str, object], repo_root: Path) -> RunRequest:
    """The command a ``run`` body asks for. Raises InvalidAgentRequestError."""
    command = _command_words(body.get("command"))
    raw_cwd = body.get("cwd")
    if raw_cwd is not None and not isinstance(raw_cwd, str):
        raise InvalidAgentRequestError("cwd must be a string")
    return RunRequest(
        command=command,
        cwd=resolve_workspace_path(repo_root, raw_cwd or ""),
        timeout_seconds=resolve_timeout_seconds(body.get("timeout_seconds")),
    )


def parse_read_file_request(body: Mapping[str, object], repo_root: Path) -> ReadFileRequest:
    """The file a ``files/read`` body names. Raises InvalidAgentRequestError."""
    raw_path = _required_path(body)
    return ReadFileRequest(raw_path=raw_path, path=resolve_workspace_path(repo_root, raw_path))


def parse_write_file_request(body: Mapping[str, object], repo_root: Path) -> WriteFileRequest:
    """The file a ``files/write`` body carries. Raises InvalidAgentRequestError."""
    raw_path = _required_path(body)
    content_b64 = body.get("content_b64")
    if not isinstance(content_b64, str):
        raise InvalidAgentRequestError("content_b64 must be a string")
    try:
        content = base64.b64decode(content_b64, validate=True)
    # A character outside ASCII is a plain ValueError, which binascii's own error is one of.
    except ValueError as exc:
        raise InvalidAgentRequestError("content_b64 is not valid base64") from exc
    return WriteFileRequest(
        raw_path=raw_path,
        path=resolve_workspace_path(repo_root, raw_path),
        content=content,
        mode=parse_file_mode(body.get("mode")),
    )


def read_workspace_file(path: Path) -> bytes | None:
    """The file's bytes, or None when there is no such file. Raises WorkspaceFileError for one that cannot be read whole."""
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise WorkspaceFileError("it is not a regular file")
        with path.open("rb") as file:
            content = file.read(MAX_READ_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkspaceFileError(exc.strerror or type(exc).__name__) from exc
    if len(content) > MAX_READ_FILE_BYTES:
        raise WorkspaceFileError(f"it is larger than {MAX_READ_FILE_BYTES} bytes; read it in pieces with a command")
    return content


def write_workspace_file(path: Path, content: bytes, mode: int) -> None:
    """Replace ``path`` through a same-directory temp file and a rename, creating its directory. Raises WorkspaceFileError."""
    # The root has no name to put a temp file beside, and is a directory like any other.
    if not path.name:
        raise WorkspaceFileError(os.strerror(errno.EISDIR))
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Created owner-only, so the bytes are never readable more widely than ``mode`` allows.
        with os.fdopen(os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
            file.write(content)
            os.fchmod(file.fileno(), mode)
        os.replace(temp_path, path)
    except OSError as exc:
        with suppress(OSError):
            temp_path.unlink()
        raise WorkspaceFileError(exc.strerror or type(exc).__name__) from exc


class RunningCommands:
    """The commands in flight, so a gateway that stops can end them instead of leaving them to run unwatched."""

    def __init__(self) -> None:
        self._processes: set[subprocess.Popen[bytes]] = set()
        self._lock = threading.Lock()

    def add(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes.add(process)

    def discard(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes.discard(process)

    def kill_all(self) -> None:
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            _kill_command(process)


def _signal_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _kill_command(process: subprocess.Popen[bytes]) -> None:
    """SIGKILL the command and everything in its process group. The command is left for its stream to reap."""
    # A reaped command's id may already name another process, so nothing is signalled after the reap.
    if process.returncode is not None:
        return
    _signal_process_group(process)
    # The command itself too, in case it left the group.
    try:
        os.kill(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # gVisor, which a workspace runs under, can miss a child that a member of the group was starting as the signal
    # went out. Once the command is dead, and while it is unreaped so that its id still names the group, a second
    # signal ends what the first one missed.
    try:
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
    except ChildProcessError:
        return
    _signal_process_group(process)


class _LineSplitter:
    """One output stream's bytes, decoded and cut into an event per line."""

    def __init__(self, event_type: str) -> None:
        self._event_type = event_type
        # Incremental, so a character split across two reads is decoded whole.
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""

    def feed(self, chunk: bytes) -> list[dict[str, object]]:
        """The events ``chunk`` completes; an empty chunk is the stream's end and flushes what is left."""
        is_end = not chunk
        self._pending += self._decoder.decode(chunk, final=is_end)
        *lines, self._pending = self._pending.split("\n")
        pieces = [f"{line}\n" for line in lines]
        while len(self._pending) >= _MAX_LINE_CHARACTERS:
            pieces.append(self._pending[:_MAX_LINE_CHARACTERS])
            self._pending = self._pending[_MAX_LINE_CHARACTERS:]
        if is_end and self._pending:
            pieces.append(self._pending)
            self._pending = ""
        return [{"type": self._event_type, "data": piece} for piece in pieces]


def _has_hung_up(caller: socket.socket) -> bool:
    """Whether the readable ``caller`` is at its end, as opposed to holding bytes nobody has read."""
    try:
        return caller.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except BlockingIOError:
        return False
    except OSError:
        return True


class _CommandOutput:
    """A command's two output pipes, read as they fill, and its caller's connection, watched for its end.

    Closed with the block that opened it; the caller's connection is left open.
    """

    def __init__(self, process: subprocess.Popen[bytes], caller: socket.socket | None) -> None:
        assert process.stdout is not None and process.stderr is not None
        self._pipes = (process.stdout, process.stderr)
        self._splitters = (_LineSplitter(STDOUT_EVENT), _LineSplitter(STDERR_EVENT))
        self._open_pipe_count = len(self._pipes)
        self.has_caller_left = False
        self._selector = selectors.DefaultSelector()
        for pipe, splitter in zip(self._pipes, self._splitters, strict=True):
            self._selector.register(pipe, selectors.EVENT_READ, splitter)
        if caller is not None:
            self._selector.register(caller, selectors.EVENT_READ, caller)

    def __enter__(self) -> "_CommandOutput":
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._selector.close()
        for pipe in self._pipes:
            pipe.close()

    @property
    def is_open(self) -> bool:
        """Whether either pipe may still carry output."""
        return self._open_pipe_count > 0

    def read_events(self, timeout_seconds: float) -> list[dict[str, object]]:
        """The events of whatever output arrives within ``timeout_seconds``; empty when none does."""
        events: list[dict[str, object]] = []
        for key, _mask in self._selector.select(timeout_seconds):
            if isinstance(key.data, socket.socket):
                # Looked at once: a connection with unread bytes would otherwise wake every wait from here on.
                self._selector.unregister(key.fileobj)
                self.has_caller_left = _has_hung_up(key.data)
                continue
            chunk = os.read(key.fd, _READ_CHUNK_BYTES)
            events.extend(key.data.feed(chunk))
            if not chunk:
                self._selector.unregister(key.fileobj)
                self._open_pipe_count -= 1
        return events

    def read_written_events(self) -> list[dict[str, object]]:
        """The events of all that is written and no event has carried yet, a line with no end included, without waiting.

        What a command that is about to be stopped has to show. Each pipe is read once, so a command that never
        stops writing cannot hold this past the moment it was called.
        """
        events = self.read_events(0.0)
        for splitter in self._splitters:
            events.extend(splitter.feed(b""))
        return events


def _exit_code_while_awaited(process: subprocess.Popen[bytes], output: _CommandOutput, deadline: float) -> int | None:
    """The command's exit code as owner-exec reports it, or None when it has not exited by ``deadline`` or its
    caller has left first.

    For a command whose output has ended and which runs on: its caller is looked at between waits, the only
    place left where a caller that has gone would show.
    """
    return_code: int | None = None
    is_awaited = True
    while is_awaited:
        remaining = deadline - time.monotonic()
        try:
            return_code = process.wait(timeout=min(max(remaining, 0.0), _CALLER_CHECK_SECONDS))
        except subprocess.TimeoutExpired:
            output.read_events(0.0)
            is_awaited = remaining > 0 and not output.has_caller_left
        else:
            is_awaited = False
    if return_code is None:
        return None
    return return_code if return_code >= 0 else _SIGNALLED_EXIT_CODE


def stream_command(
    request: RunRequest, running: RunningCommands, caller: socket.socket | None = None
) -> Generator[dict[str, object], None, None]:
    """Run the command and yield its events as they happen: one per line of output, then one ``exit`` event.

    The command runs directly (no shell), with no input, in a process group of
    its own. It is finished when it has exited and nothing it started still
    holds its output open, which is what a shell waits for at the end of a
    pipe. When its time runs out first, the whole group is killed and the exit
    event says ``timed_out``; what it had written by then comes before that
    event, a line it had not ended included.

    A command nobody is waiting for is ended the same way, with no exit event:
    when this generator is closed, which is how a server reports an answer it
    could not deliver, and when ``caller``, the connection the answer goes out
    on, reaches its end, which is how a command that prints nothing learns its
    caller has gone.
    """
    try:
        process = subprocess.Popen(
            request.command,
            cwd=str(request.cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            process_group=0,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        yield {"type": EXIT_EVENT, "code": None, "error": str(exc)}
        return
    deadline = time.monotonic() + request.timeout_seconds
    exit_code: int | None = None
    events_at_timeout: list[dict[str, object]] = []
    running.add(process)
    try:
        with _CommandOutput(process, caller) as output:
            while output.is_open and not output.has_caller_left and (remaining := deadline - time.monotonic()) > 0:
                yield from output.read_events(remaining)
            if not output.has_caller_left and not output.is_open:
                exit_code = _exit_code_while_awaited(process, output, deadline)
            if output.has_caller_left:
                return
            if exit_code is None:
                events_at_timeout = output.read_written_events()
    finally:
        if exit_code is None:
            _kill_command(process)
            process.wait()
        running.discard(process)
    if exit_code is None:
        yield from events_at_timeout
        yield {"type": EXIT_EVENT, "code": None, "timed_out": True}
    else:
        yield {"type": EXIT_EVENT, "code": exit_code}
