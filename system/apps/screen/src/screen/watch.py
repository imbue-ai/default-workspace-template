"""The ``screen-watch`` supervisord event listener (docs/system/specs/screen/spec.md section 9.3): when any member
of the stack's group leaves RUNNING, stop the other members, record a ``screen_stopped`` event, and tell the service.

stdout carries the supervisord event-listener protocol (READY / RESULT handshakes) and nothing else; diagnostics go
to stderr. Every event is answered OK: a FAIL would only make supervisord rebuffer and replay an event there is
nothing more to do with.
"""

import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from screen.data_types import ScreenStopped
from screen.errors import SupervisorError
from screen.events import ScreenEventType
from screen.events import append_screen_event
from screen.interfaces import SupervisorInterface
from screen.primitives import SERVICE_HOST
from screen.primitives import SERVICE_PORT
from screen.primitives import STACK_GROUP
from screen.primitives import STACK_PROGRAMS
from screen.supervisor import SupervisorctlClient

# Where the service is told; it broadcasts ``stopped`` to every viewer.
STOPPED_NOTIFICATION_PATH: Final[str] = "/api/internal/stopped"
_NOTIFY_TIMEOUT_SECONDS: Final[float] = 5.0
DEFAULT_STATE_DIR: Final[Path] = Path("data/.state/screen")

_HANDLED_EVENTS: Final[frozenset[str]] = frozenset({"PROCESS_STATE_EXITED", "PROCESS_STATE_FATAL"})


class GroupEndDecision(FrozenModel):
    """What one supervisord event calls for: the members to stop and the record to write, or nothing."""

    stopped: ScreenStopped | None = Field(description="The member that left and why; None when the event is not ours")
    members_to_stop: tuple[str, ...] = Field(description="The other members of the group")


@pure
def parse_token_fields(line: str) -> dict[str, str]:
    """Parse a supervisord header or payload line of space-separated ``key:value`` tokens."""
    fields: dict[str, str] = {}
    for token in line.split():
        key, separator, value = token.partition(":")
        if separator:
            fields[key] = value
    return fields


@pure
def decide_group_end(event_name: str, payload: str) -> GroupEndDecision:
    """Whether a supervisord process-state event ends the group, and what to do about it.

    Only an EXITED or FATAL event of a member of the stack's group counts; a stop the service asked for leaves
    STOPPED events, which are not ours to react to.
    """
    fields = parse_token_fields(payload)
    program = fields.get("processname", "")
    group = fields.get("groupname", "")
    if event_name not in _HANDLED_EVENTS or group != STACK_GROUP or program not in STACK_PROGRAMS:
        return GroupEndDecision(stopped=None, members_to_stop=())
    if event_name == "PROCESS_STATE_FATAL":
        reason = f"{program} could not be started"
    elif fields.get("expected") == "1":
        reason = f"{program} exited"
    else:
        reason = f"{program} exited unexpectedly"
    # The session goes before the X server it runs on, so a stop never looks like a second unexpected exit.
    return GroupEndDecision(
        stopped=ScreenStopped(member=program, reason=reason),
        members_to_stop=tuple(member for member in reversed(STACK_PROGRAMS) if member != program),
    )


def notify_service(stopped: ScreenStopped, service_url: str) -> None:
    """Tell the service the group ended; a service that is itself down will find out on its next start."""
    request = urllib.request.Request(
        service_url + STOPPED_NOTIFICATION_PATH,
        data=stopped.model_dump_json().encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_NOTIFY_TIMEOUT_SECONDS):
            pass
    except (urllib.error.URLError, OSError) as e:
        logger.warning("Could not tell the Screen service that {}: {}", stopped.reason, e)


def end_group(
    decision: GroupEndDecision,
    supervisor: SupervisorInterface,
    state_dir: Path,
    notify: Callable[[ScreenStopped], None],
) -> None:
    """Carry out a decision: stop the other members, record the event, notify the service."""
    if decision.stopped is None:
        return
    logger.warning("The Screen's {}; stopping the rest of the group", decision.stopped.reason)
    for member in decision.members_to_stop:
        try:
            supervisor.stop_program(member)
        except SupervisorError as e:
            logger.warning("Could not stop {} after the group ended: {}", member, e)
    append_screen_event(
        state_dir,
        ScreenEventType.SCREEN_STOPPED,
        {"member": decision.stopped.member, "reason": decision.stopped.reason},
    )
    notify(decision.stopped)


def _write_protocol(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def serve_one_event(supervisor: SupervisorInterface, state_dir: Path, service_url: str) -> bool:
    """One READY -> event -> RESULT protocol round; False on EOF (supervisord closed the pipe)."""
    _write_protocol("READY\n")
    header_line = sys.stdin.readline()
    if not header_line:
        return False
    header = parse_token_fields(header_line)
    length_text = header.get("len", "")
    payload = sys.stdin.read(int(length_text)) if length_text.isdigit() else ""
    decision = decide_group_end(header.get("eventname", ""), payload)
    end_group(decision, supervisor, state_dir, lambda stopped: notify_service(stopped, service_url))
    _write_protocol("RESULT 2\nOK")
    return True


def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    supervisor = SupervisorctlClient()
    service_url = f"http://{SERVICE_HOST}:{SERVICE_PORT}"
    while serve_one_event(supervisor, DEFAULT_STATE_DIR, service_url):
        pass


if __name__ == "__main__":
    main()
