"""Concrete fakes of the library's interfaces, shared by its tests."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from pydantic import Field

from auth_sandbox.image_build import BuildSandboxInterface
from auth_sandbox.interfaces import (
    FinishedCommand,
    ReencryptRunnerInterface,
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
    """One re-encrypt invocation as the fake saw it."""

    argv: tuple[str, ...] = Field(description="The command line")
    environment: dict[str, str] = Field(description="Its environment")
    stdin_text: str = Field(description="What was on its stdin")
    working_directory: Path = Field(description="Where it ran")


class FakeReencryptRunner(ReencryptRunnerInterface):
    """Stands in for the CLI: records the run and writes the files a real re-encrypt would into the destination.

    The destination is the argument after ``re-encrypt``; the files written are the store, its format stamp, and
    the browser state when ``--browser-state`` was asked for and ``has_browser_state`` says the source holds one.
    """

    returncode: int = Field(default=0, description="The exit code to answer")
    stderr: str = Field(default="", description="What the fake prints on stderr")
    has_browser_state: bool = Field(
        default=True, description="Whether the source store carries a browser state"
    )
    runs: list[RecordedRun] = Field(
        default_factory=list, description="Every run, in order"
    )

    def run(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
        stdin_text: str,
        working_directory: Path,
    ) -> FinishedCommand:
        self.runs.append(
            RecordedRun(
                argv=tuple(argv),
                environment=dict(environment),
                stdin_text=stdin_text,
                working_directory=working_directory,
            )
        )
        if self.returncode != 0:
            return FinishedCommand(
                returncode=self.returncode, stdout="", stderr=self.stderr
            )
        destination = Path(argv[list(argv).index("re-encrypt") + 1])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "credentials.json.enc").write_bytes(
            b"enc:" + stdin_text.encode("utf-8")
        )
        (destination / "data-format-version").write_text("4")
        if "--browser-state" in argv and self.has_browser_state:
            (destination / "browser_state.json.enc").write_bytes(
                b"state:" + stdin_text.encode("utf-8")
            )
        return FinishedCommand(returncode=0, stdout="", stderr="")


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
