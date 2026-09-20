import asyncio
import time
from abc import ABC
from abc import abstractmethod

from imbue.imbue_common.mutable_model import MutableModel


class ClockInterface(MutableModel, ABC):
    """The passage of time as a trial's waits experience it: what the wall clock reads, and waiting.

    Every deadline the driver enforces is a wall-clock reading plus a budget, and every poll loop
    that enforces one waits between attempts, so those two operations are the whole seam a test
    needs in order to drive a budget without racing the machine it runs on.
    """

    @abstractmethod
    def now(self) -> float:
        """The reading every deadline is measured against, in seconds since the epoch."""

    @abstractmethod
    async def sleep(self, seconds: float) -> None:
        """Wait this long, yielding the event loop so the other trials on it keep polling."""


class RealClock(ClockInterface):
    """The process's own clock: the system wall clock, and real waits on the event loop."""

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
