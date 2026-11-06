"""What the command and the app both do: the one place a routine is listed, changed, run, or removed."""

from datetime import timedelta
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from routines.chat_app import HttpChatApp
from routines.clock import SystemClock
from routines.data_types import RoutineDefinition
from routines.data_types import RoutineDetail
from routines.data_types import RoutineListing
from routines.data_types import RoutineRow
from routines.data_types import RunDetail
from routines.data_types import RunRecord
from routines.data_types import RunResult
from routines.data_types import RunStartReceipt
from routines.data_types import ScheduleChange
from routines.definitions import FileDefinitionStore
from routines.errors import ChatAppError
from routines.errors import DefinitionValidationError
from routines.errors import RunInFlightError
from routines.errors import RunNotFoundError
from routines.interfaces import ChatAppInterface
from routines.interfaces import ClockInterface
from routines.interfaces import DefinitionStoreInterface
from routines.interfaces import LedgerInterface
from routines.interfaces import NotifierInterface
from routines.interfaces import StateStoreInterface
from routines.ledger import FileLedger
from routines.locks import held_lock
from routines.notifications import GatewayNotifier
from routines.paths import RoutinesPaths
from routines.presentation import build_listing
from routines.presentation import build_row
from routines.presentation import fold_run_events
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.primitives import RunTrigger
from routines.registry import app_display_names
from routines.registry import registry_path_of
from routines.repository import GitRepository
from routines.runner import Runner
from routines.runner import is_run_process_alive
from routines.runner import read_result
from routines.runner import spawn_run_process
from routines.state import FileStateStore
from routines.tails import read_tail

DEFAULT_RUN_LIMIT: Final[int] = 25
# How long a manual start waits for the spawned run to take hold (a fresh interpreter importing the library).
RUN_START_WAIT_SECONDS: Final[float] = 5.0
RUN_START_POLL_SECONDS: Final[float] = 0.05
LOG_TAIL_BYTES: Final[int] = 200_000


class RoutinesService(MutableModel):
    """The library's front door, over the stores and the runner."""

    paths: RoutinesPaths = Field(frozen=True, description="Where everything lives")
    registry_path: Path = Field(frozen=True, description="The app registry, for the apps' display names")
    definitions: DefinitionStoreInterface = Field(frozen=True, description="The definition files")
    states: StateStoreInterface = Field(frozen=True, description="The state files")
    ledger: LedgerInterface = Field(frozen=True, description="The run events")
    chat_app: ChatAppInterface = Field(frozen=True, description="The chat app")
    clock: ClockInterface = Field(frozen=True, description="The time")
    runner: Runner = Field(frozen=True, description="What runs routines")

    def listing(self) -> RoutineListing:
        now = self.clock.now()
        zone = self.clock.local_zone()
        rows = [
            build_row(loaded, self.states.read(loaded.routine_id), now, zone)
            for loaded in self.definitions.list_routines()
        ]
        return build_listing(rows, app_display_names(self.registry_path), now, zone)

    def row(self, routine_id: RoutineId) -> RoutineRow:
        loaded = self.definitions.load(routine_id)
        return build_row(
            loaded,
            self.states.read(routine_id),
            self.clock.now(),
            self.clock.local_zone(),
        )

    def detail(self, routine_id: RoutineId, run_limit: int = DEFAULT_RUN_LIMIT) -> RoutineDetail:
        loaded = self.definitions.load(routine_id)
        now = self.clock.now()
        row = build_row(loaded, self.states.read(routine_id), now, self.clock.local_zone())
        runs = fold_run_events(self.ledger.recent_events(routine_id, run_limit), now)
        return RoutineDetail(
            row=row,
            definition=loaded.definition,
            definition_path=str(self.paths.definition_path(routine_id)),
            runs=tuple(runs),
        )

    def runs(self, routine_id: RoutineId, run_limit: int = DEFAULT_RUN_LIMIT) -> list[RunRecord]:
        self.definitions.load(routine_id)
        return fold_run_events(self.ledger.recent_events(routine_id, run_limit), self.clock.now())

    def run_detail(self, routine_id: RoutineId, run_id: RunId) -> RunDetail:
        self.definitions.load(routine_id)
        records = fold_run_events(self.ledger.events_of_run(routine_id, run_id), self.clock.now())
        if not records:
            raise RunNotFoundError(run_id)
        log, is_truncated = self.log_tail(routine_id, run_id)
        result: RunResult = read_result(self.paths.result_path(routine_id, run_id))
        return RunDetail(run=records[0], log=log, is_log_truncated=is_truncated, links=result.links)

    def run_exists(self, routine_id: RoutineId, run_id: RunId) -> bool:
        """Whether the ledger knows the run; raises RoutineNotFoundError for a routine there is none of."""
        self.definitions.load(routine_id)
        return len(self.ledger.events_of_run(routine_id, run_id)) > 0

    def log_tail(self, routine_id: RoutineId, run_id: RunId) -> tuple[str, bool]:
        """The run's log, or its last part when it is long; whether it was cut."""
        path = self.paths.run_log_path(routine_id, run_id)
        if not path.is_file():
            return "", False
        return read_tail(path, LOG_TAIL_BYTES)

    def last_run_id(self, routine_id: RoutineId) -> RunId | None:
        state = self.states.read(routine_id)
        if state.current_run is not None:
            return state.current_run.run_id
        return state.last_run.run_id if state.last_run is not None else None

    def set_enabled(self, routine_id: RoutineId, is_enabled: bool) -> None:
        self.definitions.load(routine_id)
        self.states.update(
            routine_id,
            lambda state: state.model_copy_update(to_update(state.field_ref().is_enabled, is_enabled)),
        )

    def add(self, routine_id: RoutineId, definition: RoutineDefinition) -> Path:
        return self.definitions.add(routine_id, definition)

    def replace(
        self,
        routine_id: RoutineId,
        definition: RoutineDefinition,
        change_description: str,
    ) -> Path:
        return self.definitions.replace(routine_id, definition, change_description)

    def change_schedule(self, routine_id: RoutineId, change: ScheduleChange) -> RoutineDefinition:
        loaded = self.definitions.load(routine_id)
        if loaded.definition is None:
            raise DefinitionValidationError(f"{routine_id} is broken ({loaded.error}); fix its file first.")
        updated = loaded.definition.model_copy_update(
            to_update(loaded.definition.field_ref().every, change.every),
            to_update(loaded.definition.field_ref().at, change.at),
            to_update(loaded.definition.field_ref().days, change.days),
            to_update(loaded.definition.field_ref().day_of_month, change.day_of_month),
            to_update(loaded.definition.field_ref().once, change.once),
        )
        validated = RoutineDefinition.model_validate(updated.model_dump())
        self.definitions.replace(routine_id, validated, "change the schedule")
        return validated

    def remove(self, routine_id: RoutineId) -> None:
        """Delete the definition and the state; raises RunInFlightError while a run is going, since the run writes
        the state it would pull away. The run lock is held for the removal, so no run starts under it. The chat
        and the state go first: a definition removal whose commit is refused still unlinks the file, and a
        retry would then find no routine to clean up after."""
        self.definitions.load(routine_id)
        with held_lock(self.paths.run_lock_path(routine_id), f"A run of {routine_id}"):
            state = self.states.read(routine_id)
            if state.current_run is not None and is_run_process_alive(state.current_run):
                raise RunInFlightError(f"A run of {routine_id} is going; wait for it to finish or stop it first.")
            if state.chat_id is not None:
                try:
                    self.chat_app.stop_chat(state.chat_id)
                except ChatAppError as e:
                    logger.warning(
                        "Removing {} but could not stop its chat {}: {}",
                        routine_id,
                        state.chat_id,
                        e,
                    )
            self.states.remove(routine_id)
            self.definitions.remove(routine_id)

    def is_running(self, routine_id: RoutineId) -> bool:
        """Whether a run is going: the state records a run whose process still runs (the record a killed runner
        left behind is the tick's to reap, not a run). The run lock is never probed: a probe takes it for an
        instant, which would refuse a run starting at that very moment."""
        current = self.states.read(routine_id).current_run
        return current is not None and is_run_process_alive(current)

    def start_run_detached(self, routine_id: RoutineId) -> RunStartReceipt:
        """A manual run in a process of its own; raises RunInFlightError when one is already going, including one
        that took the routine between this check and the spawn (the spawned run then stops at the lock)."""
        loaded = self.definitions.load(routine_id)
        if loaded.definition is None:
            raise DefinitionValidationError(f"{routine_id} cannot run: {loaded.error}")
        if self.is_running(routine_id):
            raise RunInFlightError(f"A run of {routine_id} is already going.")
        run_id = RunId.generate()
        spawn_run_process(self.paths, routine_id, RunTrigger.MANUAL, run_id)
        self._wait_for_run_to_take_hold(routine_id, run_id)
        return RunStartReceipt(routine_id=routine_id, run_id=run_id)

    def _wait_for_run_to_take_hold(self, routine_id: RoutineId, run_id: RunId) -> None:
        """Wait, for a bounded time, until the spawned run has its books: the receipt then means a run is going,
        so a page that reads the routine right after it sees the run rather than a gap. Raises RunInFlightError
        when another run took the routine instead, whose lock the spawned run stops at."""
        deadline = self.clock.now() + timedelta(seconds=RUN_START_WAIT_SECONDS)
        while self.clock.now() < deadline:
            state = self.states.read(routine_id)
            current = state.current_run
            if current is not None and current.run_id == run_id:
                return
            if state.last_run is not None and state.last_run.run_id == run_id:
                return
            if current is not None and is_run_process_alive(current):
                raise RunInFlightError(f"A run of {routine_id} is already going.")
            self.clock.pause(RUN_START_POLL_SECONDS)
        logger.warning(
            "The run {} of {} had not taken hold after {} seconds", run_id, routine_id, RUN_START_WAIT_SECONDS
        )


def build_service(paths: RoutinesPaths, environ: dict[str, str]) -> RoutinesService:
    """The service over the real stores, the chat app on loopback, the gateway, and the machine's clock."""
    clock = SystemClock()
    registry_path = registry_path_of(paths.repo_root, environ)
    repository = GitRepository(repo_root=paths.repo_root, clock=clock)
    definitions = FileDefinitionStore(paths=paths, repository=repository)
    states = FileStateStore(paths=paths)
    ledger = FileLedger(paths=paths)
    chat_app = HttpChatApp(repo_root=paths.repo_root, registry_path=registry_path, environ=environ, clock=clock)
    notifier: NotifierInterface = GatewayNotifier(environ=environ)
    runner = Runner(
        paths=paths,
        definitions=definitions,
        states=states,
        ledger=ledger,
        chat_app=chat_app,
        notifier=notifier,
        clock=clock,
    )
    return RoutinesService(
        paths=paths,
        registry_path=registry_path,
        definitions=definitions,
        states=states,
        ledger=ledger,
        chat_app=chat_app,
        clock=clock,
        runner=runner,
    )
