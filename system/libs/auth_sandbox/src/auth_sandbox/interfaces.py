"""The two things the commands do through something injectable: drive latchkey's sign-in flows, and run its
``auth re-encrypt``."""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from pydantic import Field


class SignInLatchkeyInterface(MutableModel, ABC):
    """The subset of ``imbue.mngr_latchkey.core.Latchkey`` the sign-in and credential commands use."""

    @abstractmethod
    def initialize(self) -> None:
        """Check the binary and write minds' part of ``config.json`` (its merge keeps the browser entry)."""

    @abstractmethod
    def auth_browser(self, service_name: str, account: str | None) -> tuple[bool, str]:
        """Run the browser sign-in with the desktop's preparation sequence; ``(is_stored, detail)``."""

    @abstractmethod
    def services_info(self, service_name: str) -> LatchkeyServiceInfo | None:
        """What latchkey knows of a service without the network; None when it knows nothing."""

    @abstractmethod
    def auth_set_credentials(
        self, service_name: str, argv: Sequence[str]
    ) -> tuple[bool, str]:
        """Store typed credentials with the ``latchkey auth set``-family argv the caller built."""


class FinishedCommand(FrozenModel):
    """What a finished ``latchkey`` invocation left behind."""

    returncode: int = Field(description="The process exit code")
    stdout: str = Field(description="What it printed on stdout")
    stderr: str = Field(description="What it printed on stderr")


class ReencryptRunnerInterface(MutableModel, ABC):
    """Runs one ``latchkey auth re-encrypt`` invocation.

    The destination key travels on stdin and the source key in the environment, so neither reaches ``argv``.
    """

    @abstractmethod
    def run(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
        stdin_text: str,
        working_directory: Path,
    ) -> FinishedCommand:
        """Run ``argv`` to completion with ``stdin_text`` on its stdin."""
