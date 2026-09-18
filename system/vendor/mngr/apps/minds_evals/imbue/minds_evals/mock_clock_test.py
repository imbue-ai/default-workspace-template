"""A concrete ClockInterface whose time moves only when the code under test waits on it, for the
driver and bridge unit tests.

Against this clock a poll loop's budget is a count of polls rather than a race with the machine: a
loaded runner cannot expire a deadline nothing has slept towards, so a test reaches its timeout path
on exactly the poll it was sized for, and a trial that waits a ten-minute budget out costs no real
time at all.
"""

from typing import Final

from pydantic import Field

from imbue.minds_evals.clock import ClockInterface
from imbue.minds_evals.clock import RealClock

# Where virtual time starts. A plausible wall-clock reading rather than zero, so a trial that
# formats its start as a date (state.json does) writes a date rather than 1970.
MANUAL_CLOCK_EPOCH_SECONDS: Final[float] = 1_800_000_000.0


class ManualClock(ClockInterface):
    """Advances only when something sleeps on it, by exactly what that sleep asked for."""

    seconds: float = Field(default=MANUAL_CLOCK_EPOCH_SECONDS, description="The current reading, in epoch seconds")
    real_clock: RealClock = Field(default_factory=RealClock, description="Where the event-loop yield comes from")

    def now(self) -> float:
        return self.seconds

    async def sleep(self, seconds: float) -> None:
        self.seconds += seconds
        # The duration is virtual but the yield is not: the environment's own awaits, and any other
        # trial sharing the loop, still have to get their turn between two polls.
        await self.real_clock.sleep(0.0)
