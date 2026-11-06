from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from datetime import datetime
from datetime import tzinfo
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from routines.data_types import LoadedRoutine
from routines.data_types import RoutineDefinition
from routines.data_types import RoutineState
from routines.data_types import RunEvent
from routines.paths import RoutinesPaths
from routines.primitives import ChatStatusWord
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.primitives import SendVerdict


class ClockInterface(MutableModel, ABC):
    """Where the time comes from, and how a wait passes: injected so tests run without waiting."""

    @abstractmethod
    def now(self) -> datetime:
        """The current moment, timezone-aware in UTC."""

    @abstractmethod
    def local_zone(self) -> tzinfo:
        """The workspace's local time zone, which schedules are written in."""

    @abstractmethod
    def pause(self, seconds: float) -> None:
        """Let the given time pass."""


class DefinitionStoreInterface(MutableModel, ABC):
    """The definition files under ``system/routines``."""

    paths: RoutinesPaths = Field(frozen=True, description="Where the definitions live")

    @abstractmethod
    def list_routines(self) -> list[LoadedRoutine]:
        """Every definition file, by id, broken ones included."""

    @abstractmethod
    def load(self, routine_id: RoutineId) -> LoadedRoutine:
        """One definition file; raises RoutineNotFoundError when there is none."""

    @abstractmethod
    def add(self, routine_id: RoutineId, definition: RoutineDefinition) -> Path:
        """Write a new definition file and commit it; raises RoutineExistsError when one exists."""

    @abstractmethod
    def replace(
        self,
        routine_id: RoutineId,
        definition: RoutineDefinition,
        change_description: str,
    ) -> Path:
        """Rewrite an existing definition file, keeping its comments, and commit it."""

    @abstractmethod
    def remove(self, routine_id: RoutineId) -> None:
        """Delete a definition file and commit the deletion; raises RoutineNotFoundError when there is none."""


class StateStoreInterface(MutableModel, ABC):
    """The per-routine state files under ``data/.state/routines``."""

    paths: RoutinesPaths = Field(frozen=True, description="Where the state lives")

    @abstractmethod
    def read(self, routine_id: RoutineId) -> RoutineState:
        """The routine's state; a fresh one when it has none."""

    @abstractmethod
    def update(self, routine_id: RoutineId, transform: Callable[[RoutineState], RoutineState]) -> RoutineState:
        """Replace the state with what transform makes of it, under the state lock; answers the new state."""

    @abstractmethod
    def remove(self, routine_id: RoutineId) -> None:
        """Delete the routine's state directory, runs included."""


class LedgerInterface(MutableModel, ABC):
    """The append-only run events under ``data/.state/routines/events/runs``."""

    paths: RoutinesPaths = Field(frozen=True, description="Where the ledger lives")

    @abstractmethod
    def append(self, event: RunEvent) -> None:
        """Write one event."""

    @abstractmethod
    def recent_events(self, routine_id: RoutineId, run_limit: int) -> list[RunEvent]:
        """The events of the routine's most recent runs, newest first, enough for run_limit runs."""

    @abstractmethod
    def events_of_run(self, routine_id: RoutineId, run_id: RunId) -> list[RunEvent]:
        """One run's events, oldest first; empty when the ledger has none."""

    @abstractmethod
    def rotate_if_large(self, now: datetime) -> bool:
        """Archive the ledger once it is past the size cap; answers whether it did."""


class RepositoryInterface(MutableModel, ABC):
    """The workspace's git repository, as far as the library commits to it."""

    repo_root: Path = Field(frozen=True, description="The checkout")

    @abstractmethod
    def commit_path(self, path: Path, message: str) -> None:
        """Commit one path's working-tree state and nothing else; raises DefinitionCommitError when it cannot."""


class ChatAppInterface(MutableModel, ABC):
    """The workspace's chat app, as far as a skill run drives a chat through it."""

    @abstractmethod
    def chat_status(self, chat_id: str) -> ChatStatusWord | None:
        """The chat's status; None when the chat app does not list the chat."""

    @abstractmethod
    def stop_chat(self, chat_id: str) -> None:
        """Stop the chat's agent."""

    @abstractmethod
    def start_chat(self, chat_id: str) -> None:
        """Start the chat's agent again."""

    @abstractmethod
    def interrupt_chat(self, chat_id: str) -> None:
        """Interrupt the chat's current turn."""

    @abstractmethod
    def send_message(self, chat_id: str, text: str) -> SendVerdict:
        """Send a message, returning once the harness has accepted it, or once the chat app has held it for the
        agent the chat is moving to (a handoff delivers it later); raises ChatAppError on a refusal."""

    @abstractmethod
    def latest_event_id(self, chat_id: str) -> str | None:
        """The id of the chat's newest transcript event; None for an empty transcript."""

    @abstractmethod
    def assistant_text_after(self, chat_id: str, cursor_event_id: str | None) -> str | None:
        """The text of the newest agent message after the cursor (the whole transcript for None); None if none."""

    @abstractmethod
    def find_chat(self, routine_id: RoutineId) -> str | None:
        """The id of an agent already carrying the routine (its label, or the retired runner's); None for none."""

    @abstractmethod
    def create_chat(self, name: str, routine_id: RoutineId, first_message: str) -> str:
        """Create the routine's chat with the message as its first; answers the chat id or raises ChatCreateError."""


class NotifierInterface(MutableModel, ABC):
    """The Imbue Studio app's notification feed, as far as the runner posts to it."""

    @abstractmethod
    def notify(self, routine_id: RoutineId, title: str, message: str) -> bool:
        """Post one notification headed by the title and landing on the routine's page; answers whether the app took it."""
