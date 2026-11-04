import json
import shutil
from collections.abc import Callable
from typing import Final

from loguru import logger

from routines.data_types import RoutineState
from routines.errors import StateRemovalError
from routines.interfaces import StateStoreInterface
from routines.locks import waited_lock
from routines.primitives import RoutineId

# Where a state file whose content does not parse is kept once a write replaces it, so the record survives.
UNREADABLE_STATE_SUFFIX: Final[str] = ".json.bad"


class FileStateStore(StateStoreInterface):
    """One JSON file per routine, rewritten whole under the routine's state lock.

    Only a missing file reads as a fresh state. One that cannot be opened raises, since a reset is the user's
    to ask for; one whose content does not parse reads as fresh so a listing goes on, and a write over it sets
    the old file aside first rather than discarding it.
    """

    def read(self, routine_id: RoutineId) -> RoutineState:
        state, _ = self._read_with_parse_error(routine_id)
        return state

    def _read_with_parse_error(self, routine_id: RoutineId) -> tuple[RoutineState, Exception | None]:
        path = self.paths.state_path(routine_id)
        if not path.is_file():
            return RoutineState(), None
        try:
            return RoutineState.model_validate_json(path.read_text(encoding="utf-8")), None
        except ValueError as e:
            logger.warning(
                "Reading {} as a fresh state: its state file does not parse ({})",
                routine_id,
                e,
            )
            return RoutineState(), e

    def update(self, routine_id: RoutineId, transform: Callable[[RoutineState], RoutineState]) -> RoutineState:
        with waited_lock(self.paths.state_lock_path(routine_id)):
            current, parse_error = self._read_with_parse_error(routine_id)
            path = self.paths.state_path(routine_id)
            if parse_error is not None:
                set_aside = path.with_suffix(UNREADABLE_STATE_SUFFIX)
                path.replace(set_aside)
                logger.warning(
                    "Kept the unreadable state file of {} at {} before writing a fresh one", routine_id, set_aside
                )
            updated = transform(current)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(updated.model_dump(mode="json"), indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
            return updated

    def remove(self, routine_id: RoutineId) -> None:
        directory = self.paths.routine_state_dir(routine_id)
        if not directory.is_dir():
            return
        try:
            shutil.rmtree(directory)
        except OSError as e:
            raise StateRemovalError(f"Could not remove all of the state of {routine_id} under {directory}: {e}") from e
