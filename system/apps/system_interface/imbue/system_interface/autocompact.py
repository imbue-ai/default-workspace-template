import threading
from collections.abc import Callable
from collections.abc import Sequence
from typing import Final

from loguru import logger

from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version

DEFAULT_SWEEP_INTERVAL_SECONDS: Final[float] = 60.0
DEFAULT_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0
_DEFAULT_MNGR_BINARY: Final[str] = "mngr"


class ChatAutoCompactor:
    """Schedules periodic context compaction checks for active chat agents.

    Runs `mngr autocompact check <agent name>` once every interval for each
    chat agent that is currently running. All collaborators are injectable for
    unit testing without subprocesses or real agents.
    """

    _list_running_chat_agent_names: Callable[[], Sequence[str]]
    _runner: Callable[..., FinishedProcess]
    _mngr_binary: str
    _interval_seconds: float
    _command_timeout_seconds: float
    _stop_event: threading.Event
    _thread: threading.Thread | None

    @classmethod
    def build(
        cls,
        list_running_chat_agent_names: Callable[[], Sequence[str]],
        runner: Callable[..., FinishedProcess] = run_local_command_modern_version,
        mngr_binary: str = _DEFAULT_MNGR_BINARY,
        interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
        command_timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> "ChatAutoCompactor":
        instance = cls.__new__(cls)
        instance._list_running_chat_agent_names = list_running_chat_agent_names
        instance._runner = runner
        instance._mngr_binary = mngr_binary
        instance._interval_seconds = interval_seconds
        instance._command_timeout_seconds = command_timeout_seconds
        instance._stop_event = threading.Event()
        instance._thread = None
        return instance

    def start(self) -> None:
        """Start the background sweep thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._run_sweep,
            daemon=True,
            name="chat-autocompact-sweep",
        )
        self._thread = thread
        thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the background sweep to stop and wait for thread termination."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def sweep(self) -> list[FinishedProcess | None]:
        """Perform one pass of autocompact checks across all running chat agents."""
        names = self._list_running_chat_agent_names()
        results: list[FinishedProcess | None] = []
        for name in names:
            if self._stop_event.is_set():
                break
            results.append(self.check_agent(name))
        return results

    def check_agent(self, agent_name: str) -> FinishedProcess | None:
        """Run `mngr autocompact check <agent_name>` for a single agent."""
        command = [self._mngr_binary, "autocompact", "check", agent_name]
        try:
            result = self._runner(
                command=command,
                cwd=None,
                is_checked=False,
                timeout=self._command_timeout_seconds,
            )
            if result.returncode != 0:
                logger.debug(
                    "Autocompact check for {} exited {}: {}",
                    agent_name,
                    result.returncode,
                    result.stderr.strip()[:300],
                )
            return result
        except (ProcessSetupError, OSError) as e:
            logger.warning("Failed to run autocompact check for {}: {}", agent_name, e)
            return None

    def _run_sweep(self) -> None:
        """Background loop executing sweeps on interval until stopped."""
        while not self._stop_event.wait(self._interval_seconds):
            self.sweep()
