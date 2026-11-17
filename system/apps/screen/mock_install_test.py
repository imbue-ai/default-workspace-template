from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel


class FakeInstall(MutableModel):
    """What the env.d unit has installed, as a test sets it: what the stack and Chromium still miss, and how the unit
    failed in this boot (all None: everything is installed)."""

    missing_stack: str | None = Field(default=None, description="What the stack still misses")
    missing_browser: str | None = Field(default=None, description="What Chromium still misses")
    failure: str | None = Field(default=None, description="How the unit failed in this boot")
    shed_pids: set[int] = Field(default_factory=set, description="The pids the memory guard just shed")
    ledger_reads_before_shed: dict[int, int] = Field(
        default_factory=dict,
        description="Pids the memory guard shed whose record lands only after this many reads of the ledger",
    )

    def was_shed(self, pid: int) -> bool:
        """Whether the ledger names ``pid`` as shed, as read now."""
        if pid in self.shed_pids:
            return True
        reads_left = self.ledger_reads_before_shed.get(pid)
        if reads_left is None:
            return False
        if reads_left == 0:
            return True
        self.ledger_reads_before_shed[pid] = reads_left - 1
        return False
