from datetime import datetime
from datetime import timedelta
from datetime import tzinfo

from pydantic import ConfigDict
from pydantic import Field

from routines.interfaces import ClockInterface


class FakeClock(ClockInterface):
    """A clock the test sets; a pause advances it instead of waiting."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    current: datetime = Field(description="What now() answers")
    zone: tzinfo = Field(description="What local_zone() answers")
    pauses: list[float] = Field(default_factory=list, description="Every pause asked for, in order")

    def now(self) -> datetime:
        return self.current

    def local_zone(self) -> tzinfo:
        return self.zone

    def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.current = self.current + timedelta(seconds=seconds)

    def advance(self, delta: timedelta) -> None:
        self.current = self.current + delta
