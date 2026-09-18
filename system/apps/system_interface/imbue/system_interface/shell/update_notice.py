"""The update notice: the rollback point the last careful-flow apply kept, and the shell's window onto it.

An apply run with ``--keep-rollback-point`` (the update-app careful flow, never update-self's
own) leaves ``data/.state/update-apply/last-good.json`` behind: what it landed, the copies it
kept, and the critical apps and supervisord programs it touched. The shell reads that record
for ``GET /api/updates/pending``, watches the file so every window learns of a change (a
rollback's progress, its outcome, the record cleared) as it lands, and runs the two verbs only a
person closes the notice with: ``confirm-last`` (drop the record, and the copies with it when no
rollback ran; a failed rollback's copies stay for an agent) and ``rollback-last`` (revert the
merge forward, restore the copies, restart the touched programs).
Both are the update-self script's own subcommands, run rather than reimplemented, so the shell
stays one reader of the apply's contract.

The rollback is launched detached, in its own session: it restarts the shell's own program when
the shell is among what the apply touched, and supervisord stops that program as a group, so a
child in the shell's group would die halfway through its own work.
"""

import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.shell.errors import UpdateNoticeCommandError
from imbue.system_interface.shell.errors import UpdateNoticeRecordError
from imbue.system_interface.shell.errors import UpdateNoticeRefusedError
from imbue.system_interface.shell.file_watch import stop_watch
from imbue.system_interface.shell.file_watch import watch_file
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# The record the apply keeps (``update_apply_contract.LastGoodRecord`` in the update-self
# scripts), and where a rollback launched from here writes what it printed.
LAST_GOOD_RECORD_REL: Final[str] = "data/.state/update-apply/last-good.json"
ROLLBACK_LOG_REL: Final[str] = "data/.state/update-apply/rollback-last.log"
UPDATE_SELF_SCRIPT_REL: Final[str] = ".agents/skills/update-self/scripts/update_self.py"

# A confirm deletes the kept copies, which can be a whole tool environment; generous, but bounded
# so a wedged script cannot hold a request thread forever.
_CONFIRM_TIMEOUT_SECONDS: Final[float] = 120.0
_CONFIRM_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 5.0

# How long a launched rollback gets to write its first progress into the record (its checks before
# that are a lock, a few file reads, and one ``git status``), and how often the record is re-read.
_ROLLBACK_START_TIMEOUT_SECONDS: Final[float] = 30.0
_ROLLBACK_START_POLL_SECONDS: Final[float] = 0.1


class UpdateNotice(FrozenModel):
    """What the windows show of the kept rollback point (the wire shape of contracts.md section 5)."""

    merge_sha: str = Field(description="The merge the apply landed")
    applied_at: float = Field(description="When the apply finished, seconds since the epoch")
    driven_by: str = Field(description="The agent (or person) that ran the apply, as it named itself")
    apps: tuple[str, ...] = Field(description="The critical apps whose program or bundle the apply changed")
    programs: tuple[str, ...] = Field(description="The supervisord programs a rollback restarts")
    needs_services_restart: bool = Field(
        description="Whether the diff reached the workspace's own setup, so a rollback restores the files but "
        "leaves the restart to an agent"
    )
    progress: str | None = Field(description="What a running rollback is doing right now")
    outcome: str | None = Field(description="How the rollback ended; the record is settled once set")

    @property
    def is_rolling_back(self) -> bool:
        return self.progress is not None and self.outcome is None

    @property
    def is_settled(self) -> bool:
        return self.outcome is not None

    def wire_json(self) -> dict[str, Any]:
        return {
            "merge_sha": self.merge_sha,
            "applied_at": self.applied_at,
            "driven_by": self.driven_by,
            "apps": list(self.apps),
            "programs": list(self.programs),
            "needs_services_restart": self.needs_services_restart,
            "progress": self.progress,
            "outcome": self.outcome,
        }


def _notice_from_record(raw: Any) -> UpdateNotice:
    if not isinstance(raw, dict):
        raise UpdateNoticeRecordError(f"expected a JSON object, got {type(raw).__name__}")
    progress = raw.get("progress")
    outcome = raw.get("outcome")
    return UpdateNotice(
        merge_sha=str(raw["merge_sha"]),
        applied_at=float(raw.get("applied_at", 0.0)),
        driven_by=str(raw.get("driven_by", "")),
        apps=tuple(str(name) for name in raw.get("apps", [])),
        programs=tuple(str(program) for program in raw.get("programs", [])),
        needs_services_restart=bool(raw.get("needs_services_restart", False)),
        progress=str(progress) if progress is not None else None,
        outcome=str(outcome) if outcome is not None else None,
    )


def read_update_notice(record_path: Path) -> UpdateNotice | None:
    """The kept rollback point, or ``None`` when there is none.

    Lenient the way the apply's own reader is: a record that will not read or parse is logged and
    treated as absent, since a notice is something to raise, never something to fail a page over,
    and the writer replaces the file atomically so a torn read is a moment's condition.
    """
    try:
        text = record_path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("update-notice: could not read {} ({}); no notice.", record_path, e)
        return None
    try:
        return _notice_from_record(json.loads(text))
    except (ValueError, KeyError, TypeError) as e:
        logger.warning("update-notice: {} is not a rollback-point record ({}); no notice.", record_path, e)
        return None


def _launch_detached(argv: list[str], cwd: Path, log_path: Path) -> Callable[[], int | None]:
    """Start ``argv`` in its own session with its output appended to ``log_path``.

    Answers a poll: the child's exit code once it has exited, ``None`` while it runs. That is all
    a caller gets, since the child is meant to outlive this process and nothing here waits on it.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return process.poll


def _log_tail_since(log_path: Path, offset: int) -> str:
    """The last line the script wrote past ``offset``: its refusal, in its own words."""
    try:
        with log_path.open("rb") as log_file:
            log_file.seek(offset)
            written = log_file.read().decode("utf-8", errors="replace")
    except OSError:
        return f"see {log_path}"
    lines = [line.strip() for line in written.splitlines() if line.strip()]
    return lines[-1] if lines else f"see {log_path}"


class UpdateNoticeWatch(MutableModel):
    """Reads the kept rollback point, announces its every change, and runs the verbs that close it."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    repo_root: Path = Field(frozen=True, description="The workspace root the record and the script live under")
    broadcaster: WebSocketBroadcaster = Field(frozen=True, description="Where a change is announced")

    _observer: Any | None = PrivateAttr(default=None)
    _last_announced: UpdateNotice | None = PrivateAttr(default=None)
    _launch_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Set by the watch on every change to the record: what a launch waits on for the script's
    # first progress. Without a running watch the launch re-reads the record on its own cadence.
    _record_changed: threading.Event = PrivateAttr(default_factory=threading.Event)

    @property
    def record_path(self) -> Path:
        return self.repo_root / LAST_GOOD_RECORD_REL

    def current(self) -> UpdateNotice | None:
        return read_update_notice(self.record_path)

    def start(self) -> None:
        self._last_announced = self.current()
        self._observer = watch_file(self.record_path, self._on_record_changed)

    def stop(self) -> None:
        stop_watch(self._observer)
        self._observer = None

    def _on_record_changed(self) -> None:
        """Announce the record as it now reads. A write raises several events (created, modified,
        closed), so the announcement is made once per distinct reading."""
        self._record_changed.set()
        notice = self.current()
        if notice == self._last_announced:
            return
        self._last_announced = notice
        self.broadcaster.broadcast_update_notice_changed(notice.wire_json() if notice is not None else None)

    def confirm(self) -> None:
        """Close the notice: the script drops the record, and the kept copies with it when no
        rollback ran on the point (a failed rollback's copies stay for an agent).

        Raises ``UpdateNoticeRefusedError`` when there is nothing to confirm or a rollback is
        running, and ``UpdateNoticeCommandError`` when the script could not run or failed; the
        record is left as the script left it either way, and the windows learn of the change
        from the watch.

        A rollback in flight is refused because confirming discards the very copies it is
        restoring from, leaving it to warn about files it could not restore and then write
        back the record it was just told to close. The band hides both verbs while a rollback
        runs, so this catches a window that has not seen the progress yet.
        """
        notice = self.current()
        if notice is None:
            raise UpdateNoticeRefusedError("There is no update notice to confirm.")
        if notice.is_rolling_back:
            raise UpdateNoticeRefusedError("A rollback is running; wait for it to finish.")
        try:
            result = run_local_command_modern_version(
                command=[sys.executable, str(self.repo_root / UPDATE_SELF_SCRIPT_REL), "confirm-last"],
                cwd=self.repo_root,
                is_checked=False,
                timeout=_CONFIRM_TIMEOUT_SECONDS,
                shutdown_timeout_sec=_CONFIRM_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except (ProcessError, OSError) as e:
            raise UpdateNoticeCommandError(f"The update notice could not be closed: {e}") from e
        if result.returncode != 0:
            raise UpdateNoticeCommandError(
                f"The update notice could not be closed (exit {result.returncode}): {result.stderr.strip()[-300:]}"
            )

    def launch_rollback(self) -> None:
        """Start the rollback and answer once it is under way; its progress and outcome reach the
        windows through the record it rewrites as it goes.

        Under way means the script has written its first progress into the record (or has
        already finished): two presses of the button from two windows land here together, and
        the second must read the first's progress rather than start a second script. The
        launches are serialized for that, and each one holds until the record says so.

        Raises ``UpdateNoticeRefusedError`` when there is nothing to roll back, a rollback is
        already running, the point was already taken back (a settled record is closed, not
        re-run), or the script refused (a dirty tree, an apply in flight), and
        ``UpdateNoticeCommandError`` when the script could not be started or never got under way.
        """
        with self._launch_lock:
            notice = self.current()
            if notice is None:
                raise UpdateNoticeRefusedError("There is no update to roll back.")
            if notice.is_rolling_back:
                raise UpdateNoticeRefusedError("A rollback is already running.")
            if notice.is_settled:
                raise UpdateNoticeRefusedError("This update was already rolled back; close the notice instead.")
            log_path = self.repo_root / ROLLBACK_LOG_REL
            log_offset = log_path.stat().st_size if log_path.exists() else 0
            try:
                poll = _launch_detached(
                    [sys.executable, str(self.repo_root / UPDATE_SELF_SCRIPT_REL), "rollback-last"],
                    cwd=self.repo_root,
                    log_path=log_path,
                )
            except OSError as e:
                raise UpdateNoticeCommandError(f"The rollback could not be started: {e}") from e
            self._wait_until_under_way(poll, notice, log_path, log_offset)

    def _wait_until_under_way(
        self, poll: Callable[[], int | None], before: UpdateNotice, log_path: Path, log_offset: int
    ) -> None:
        deadline = time.monotonic() + _ROLLBACK_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            self._record_changed.clear()
            # The exit code first, then the record: a script that has exited wrote everything it was
            # going to, so a record read after the poll says whether it got under way (and, when it
            # failed straight after, how it went), and only an exit that left the record untouched is
            # a refusal.
            returncode = poll()
            if self.current() != before:
                return
            if returncode is not None:
                if returncode == 0:
                    return
                raise UpdateNoticeRefusedError(
                    f"The rollback was refused (exit {returncode}): {_log_tail_since(log_path, log_offset)}"
                )
            self._record_changed.wait(timeout=_ROLLBACK_START_POLL_SECONDS)
        raise UpdateNoticeCommandError(
            f"The rollback did not get under way within {_ROLLBACK_START_TIMEOUT_SECONDS:g}s; see {log_path}."
        )
