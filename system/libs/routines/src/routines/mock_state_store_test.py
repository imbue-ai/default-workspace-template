import os
from datetime import datetime
from datetime import timezone

from pydantic import Field

from chat_background_tasks.markers import process_start_time
from imbue.imbue_common.model_update import to_update
from routines.data_types import CurrentRun
from routines.data_types import RoutineState
from routines.errors import StateRemovalError
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.primitives import RunTrigger
from routines.state import FileStateStore


class RefusingRemovalStateStore(FileStateStore):
    """A state store whose directories cannot be deleted whole, as a file the process may not remove leaves them."""

    def remove(self, routine_id: RoutineId) -> None:
        raise StateRemovalError(f"Could not remove all of the state of {routine_id}: a file could not be deleted")


class OneRefusingStateStore(FileStateStore):
    """A state store whose file for one routine cannot be opened."""

    refused_id: RoutineId = Field(description="The routine whose state read fails")

    def read(self, routine_id: RoutineId) -> RoutineState:
        if routine_id == self.refused_id:
            raise PermissionError(f"{routine_id}: state.json is not readable")
        return super().read(routine_id)


class StaleReadStateStore(FileStateStore):
    """Answers one read with a state read just before the run finished, as a reaper racing the run sees it."""

    stale: RoutineState | None = Field(default=None, description="The state the next read answers, once")

    def read(self, routine_id: RoutineId) -> RoutineState:
        if self.stale is not None:
            stale, self.stale = self.stale, None
            return stale
        return super().read(routine_id)


class StateStoreWithARunArriving(FileStateStore):
    """Answers the first read with no run in flight and every later one with another, live run of the routine:
    the window in which a scheduled run takes the routine between a manual start's check and its spawn."""

    reads_served: int = Field(default=0, description="How many reads have been answered")

    def read(self, routine_id: RoutineId) -> RoutineState:
        state = super().read(routine_id)
        self.reads_served += 1
        if self.reads_served == 1:
            return state
        arrived = CurrentRun(
            run_id=RunId.generate(),
            pid=os.getpid(),
            pid_start=process_start_time(os.getpid()),
            trigger=RunTrigger.SCHEDULED,
            started_at=datetime.now(timezone.utc),
        )
        return state.model_copy_update(to_update(state.field_ref().current_run, arrived))
