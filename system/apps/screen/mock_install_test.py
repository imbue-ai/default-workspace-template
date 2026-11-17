from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel


class FakeInstall(MutableModel):
    """What the env.d unit has installed, as a test sets it: what the stack and Chromium still miss, and how the unit
    failed in this boot (all None: everything is installed)."""

    missing_stack: str | None = Field(default=None, description="What the stack still misses")
    missing_browser: str | None = Field(default=None, description="What Chromium still misses")
    failure: str | None = Field(default=None, description="How the unit failed in this boot")
    shed_pids: set[int] = Field(default_factory=set, description="The pids the memory guard just shed")
