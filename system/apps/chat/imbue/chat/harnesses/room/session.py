"""A room's control surface: a send appends the message to the room's transcript; there is no turn to interrupt."""

from collections.abc import Callable

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.interrupt import IsCompactionInFlight
from imbue.chat.harnesses.interrupt import PressChord
from imbue.chat.harnesses.interrupt import RestartProcess
from imbue.chat.harnesses.interrupt import SettleActivity
from imbue.chat.harnesses.room.transcript import append_record
from imbue.chat.harnesses.room.transcript import person_step_record
from imbue.chat.harnesses.room.transcript import transcript_path
from imbue.chat.harnesses.session import AgentHarnessSession
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.harnesses.session import SessionDeps
from imbue.chat.harnesses.session import ShoulderTapOutcome
from imbue.chat.harnesses.session_watcher import AgentSessionWatcher


class RoomHarnessSession(AgentHarnessSession):
    """The session of a room: every message sent to it is appended to its transcript as a person's step, at once."""

    @classmethod
    def build(cls, deps: SessionDeps) -> "RoomHarnessSession":
        self = cls.__new__(cls)
        self._deps = deps
        return self

    def is_starting_up(self) -> bool:
        return False

    def send(self, text: str, message_id: str, is_resent_when_unsure: bool = True) -> SendOutcome:
        # The append is this process's own write, never an unconfirmed delivery, so there is nothing to send again.
        state_dir = self._deps.state_dir
        append_record(transcript_path(state_dir), state_dir.name, person_step_record(text, message_id))
        return SendOutcome.OK

    def shoulder_tap(
        self,
        agent_info: AgentInfo,
        watcher: AgentSessionWatcher,
        press_chord: Callable[[], bool],
        send_recovery: Callable[[str], bool],
    ) -> ShoulderTapOutcome:
        return ShoulderTapOutcome(
            status="unsupported", error_detail="A chat room has no turn to tap", error_status_code=400
        )

    def interrupt_to_composer(
        self,
        agent_info: AgentInfo,
        watcher: AgentSessionWatcher,
        restart_process: RestartProcess,
        settle_activity: SettleActivity,
        press_chord: PressChord,
        is_compaction_in_flight: IsCompactionInFlight,
    ) -> str:
        return ""
