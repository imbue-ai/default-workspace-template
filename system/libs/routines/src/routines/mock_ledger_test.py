from pydantic import Field

from routines.data_types import RunEvent
from routines.data_types import RunStartedEvent
from routines.interfaces import StateStoreInterface
from routines.ledger import FileLedger
from routines.primitives import RunId


class StateWatchingLedger(FileLedger):
    """The file ledger, noting which run the state names at the moment each start is appended."""

    states: StateStoreInterface = Field(frozen=True, description="The state the runner writes beside the ledger")
    run_in_flight_at_start: list[RunId | None] = Field(
        default_factory=list, description="Per start appended, the run the state named then"
    )

    def append(self, event: RunEvent) -> None:
        if isinstance(event, RunStartedEvent):
            current = self.states.read(event.routine_id).current_run
            self.run_in_flight_at_start.append(current.run_id if current is not None else None)
        super().append(event)
