"""The real implementations: ``imbue.mngr_latchkey.core.Latchkey`` behind the sign-in interface, a subprocess behind
the latchkey runner, and the machine's clock."""

import subprocess
import threading
import time
from collections.abc import Mapping, Sequence

from imbue.mngr_latchkey.core import Latchkey, LatchkeyServiceInfo
from pydantic import Field

from auth_sandbox.errors import LatchkeyCommandError, MissingMaterialError
from auth_sandbox.interfaces import (
    ClockInterface,
    FinishedCommand,
    LatchkeyRunnerInterface,
    SignInLatchkeyInterface,
)
from auth_sandbox.template_config import merge_minds_config

# The queries read a local store offline; the desktop budgets a few seconds for one and this is generous.
_QUERY_TIMEOUT_SECONDS = 60.0


class LatchkeyAdapter(SignInLatchkeyInterface):
    """The desktop's ``Latchkey`` wrapper, pointed at the sandbox's directory.

    The wrapper reads the encryption key through ``LATCHKEY_ENCRYPTION_KEY`` in the process environment (its
    operator override), which the command sets from the sandbox's key file before building this. ``initialize``
    is the wrapper's config merge without its ``latchkey --version`` run (``template_config``).
    """

    latchkey: Latchkey = Field(
        frozen=True,
        description="The wrapper, built with the sandbox's directory and binary",
    )

    def initialize(self) -> None:
        merge_minds_config(self.latchkey.latchkey_directory)

    def auth_browser(self, service_name: str, account: str | None) -> tuple[bool, str]:
        return self.latchkey.auth_browser(service_name, account=account)

    def services_info(self, service_name: str) -> LatchkeyServiceInfo | None:
        return self.latchkey.services_info(service_name, is_offline=True)

    def auth_set_credentials(
        self, service_name: str, argv: Sequence[str]
    ) -> tuple[bool, str]:
        return self.latchkey.auth_set_credentials(service_name, argv)


class SubprocessLatchkeyRunner(LatchkeyRunnerInterface):
    """Runs the CLI as a child process: a binary that cannot start is a missing material, and a child that outlives
    its budget is a command error."""

    def run(
        self, argv: Sequence[str], environment: Mapping[str, str]
    ) -> FinishedCommand:
        try:
            completed = subprocess.run(
                list(argv),
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_QUERY_TIMEOUT_SECONDS,
                check=False,
            )
        except OSError as e:
            raise MissingMaterialError(f"could not run {argv[0]}: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise LatchkeyCommandError(f"{argv[0]} did not finish: {e}") from e
        return FinishedCommand(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class SystemClock(ClockInterface):
    """The machine's clock; a pause is an uncontested wait on an event nobody sets."""

    def now(self) -> float:
        return time.time()

    def pause(self, seconds: float) -> None:
        threading.Event().wait(seconds)
