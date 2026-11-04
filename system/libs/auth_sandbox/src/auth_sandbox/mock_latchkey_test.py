"""Concrete fakes of the library's interfaces, shared by its tests."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from pydantic import Field

from auth_sandbox.errors import AuthSandboxError
from auth_sandbox.image_build import BuildSandboxInterface
from auth_sandbox.interfaces import (
    ClockInterface,
    FinishedCommand,
    LatchkeyRunnerInterface,
    SignInLatchkeyInterface,
)


class FakeLatchkey(SignInLatchkeyInterface):
    """Records what the commands ask of latchkey and answers what the test configured."""

    is_sign_in_stored: bool = Field(
        default=True, description="What auth_browser answers"
    )
    sign_in_detail: str = Field(
        default="", description="The failure detail auth_browser answers"
    )
    is_set_stored: bool = Field(
        default=True, description="What auth_set_credentials answers"
    )
    set_detail: str = Field(
        default="", description="The failure detail auth_set_credentials answers"
    )
    service_info: LatchkeyServiceInfo | None = Field(
        default=None, description="What services_info answers"
    )
    initialized_count: int = Field(
        default=0, description="How many times initialize ran"
    )
    browser_calls: list[tuple[str, str | None]] = Field(
        default_factory=list, description="(service, account) per call"
    )
    set_calls: list[tuple[str, tuple[str, ...]]] = Field(
        default_factory=list, description="(service, argv) per call"
    )
    info_calls: list[str] = Field(
        default_factory=list, description="The services asked about"
    )

    def initialize(self) -> None:
        self.initialized_count += 1

    def auth_browser(self, service_name: str, account: str | None) -> tuple[bool, str]:
        self.browser_calls.append((service_name, account))
        return self.is_sign_in_stored, self.sign_in_detail

    def services_info(self, service_name: str) -> LatchkeyServiceInfo | None:
        self.info_calls.append(service_name)
        return self.service_info

    def auth_set_credentials(
        self, service_name: str, argv: Sequence[str]
    ) -> tuple[bool, str]:
        self.set_calls.append((service_name, tuple(argv)))
        return self.is_set_stored, self.set_detail


class RecordedRun(FrozenModel):
    """One latchkey invocation as the fake saw it."""

    argv: tuple[str, ...] = Field(description="The command line")
    environment: dict[str, str] = Field(description="Its environment")


class FakeLatchkeyRunner(LatchkeyRunnerInterface):
    """Stands in for the CLI: records each run and answers what the test configured."""

    answer: FinishedCommand = Field(
        default=FinishedCommand(returncode=0, stdout="{}", stderr=""),
        description="What every run answers",
    )
    runs: list[RecordedRun] = Field(
        default_factory=list, description="Every run, in order"
    )

    def run(
        self, argv: Sequence[str], environment: Mapping[str, str]
    ) -> FinishedCommand:
        self.runs.append(RecordedRun(argv=tuple(argv), environment=dict(environment)))
        return self.answer


class ScheduledWrite(FrozenModel):
    """A file the fake clock writes once its time comes, as the connector would."""

    at: float = Field(description="Unix seconds")
    path: Path = Field(description="Where")
    text: str = Field(description="What")


class FakeClockExhaustedError(AuthSandboxError):
    """The code under test kept waiting after the fake clock had nothing left to happen."""


class FakeClock(ClockInterface):
    """A clock that a pause advances, writing each scheduled file when its time comes; it refuses to pause past
    ``limit`` so a wait that would never end fails the test instead of hanging it."""

    current: float = Field(default=1_000_000.0, description="Unix seconds now")
    limit: float = Field(
        default=1_000_000.0 + 86_400, description="The time no pause may pass"
    )
    scheduled: list[ScheduledWrite] = Field(
        default_factory=list, description="Writes still to happen"
    )
    pauses: list[float] = Field(default_factory=list, description="Every pause asked")

    def now(self) -> float:
        return self.current

    def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        if self.current + seconds > self.limit:
            raise FakeClockExhaustedError(
                f"paused past {self.limit} with nothing left to happen"
            )
        self.current += seconds
        due = [write for write in self.scheduled if write.at <= self.current]
        self.scheduled = [write for write in self.scheduled if write.at > self.current]
        for write in due:
            write.path.write_text(write.text)


class FakeBuildSandbox(BuildSandboxInterface):
    """A build sandbox that records what it is asked and fails the commands the test names."""

    failing_command_fragments: tuple[str, ...] = Field(
        default=(),
        description="A command containing any of these answers a non-zero exit",
    )
    image_id: str = Field(default="im-fake", description="What snapshot answers")
    commands: list[str] = Field(
        default_factory=list, description="Every command run, in order"
    )
    snapshot_count: int = Field(default=0, description="How many snapshots were taken")

    def run(self, command: str, timeout_seconds: int) -> int:
        self.commands.append(command)
        return (
            1
            if any(fragment in command for fragment in self.failing_command_fragments)
            else 0
        )

    def snapshot(self, timeout_seconds: int) -> str:
        self.snapshot_count += 1
        return self.image_id
