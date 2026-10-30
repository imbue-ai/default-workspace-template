"""The real implementations: ``imbue.mngr_latchkey.core.Latchkey`` behind the sign-in interface, and a subprocess
behind the re-encrypt runner."""

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from imbue.mngr_latchkey.core import Latchkey, LatchkeyServiceInfo
from pydantic import Field

from auth_sandbox.errors import LatchkeyCommandError
from auth_sandbox.interfaces import (
    FinishedCommand,
    ReencryptRunnerInterface,
    SignInLatchkeyInterface,
)

# ``auth re-encrypt`` is a local file rewrite; the desktop budgets five seconds for one and this is generous.
_REENCRYPT_TIMEOUT_SECONDS = 60.0


class LatchkeyAdapter(SignInLatchkeyInterface):
    """The desktop's ``Latchkey`` wrapper, pointed at the sandbox's directory.

    The wrapper reads the encryption key through ``LATCHKEY_ENCRYPTION_KEY`` in the process environment (its
    operator override), which the command sets from the sandbox's key file before building this.
    """

    latchkey: Latchkey = Field(
        frozen=True,
        description="The wrapper, built with the sandbox's directory and binary",
    )

    def initialize(self) -> None:
        self.latchkey.initialize()

    def auth_browser(self, service_name: str, account: str | None) -> tuple[bool, str]:
        return self.latchkey.auth_browser(service_name, account=account)

    def services_info(self, service_name: str) -> LatchkeyServiceInfo | None:
        return self.latchkey.services_info(service_name, is_offline=True)

    def auth_set_credentials(
        self, service_name: str, argv: Sequence[str]
    ) -> tuple[bool, str]:
        return self.latchkey.auth_set_credentials(service_name, argv)


class SubprocessReencryptRunner(ReencryptRunnerInterface):
    """Runs the CLI as a child process; a child that cannot start or outlives its budget is a command error."""

    def run(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
        stdin_text: str,
        working_directory: Path,
    ) -> FinishedCommand:
        try:
            completed = subprocess.run(
                list(argv),
                input=stdin_text,
                env=dict(environment),
                cwd=working_directory,
                capture_output=True,
                text=True,
                timeout=_REENCRYPT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LatchkeyCommandError(f"could not run {argv[0]}: {e}") from e
        return FinishedCommand(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
