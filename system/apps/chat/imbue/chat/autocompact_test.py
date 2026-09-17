import inspect
import threading
from collections.abc import Sequence

from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mngr.utils.polling import poll_until
from imbue.chat.autocompact import _DEFAULT_CHECK_CONCURRENCY
from imbue.chat.autocompact import ChatAutoCompactor


def _make_finished_process(
    command: Sequence[str],
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> FinishedProcess:
    return FinishedProcess(
        command=tuple(command),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        is_timed_out=False,
        is_output_already_logged=False,
    )


def test_default_check_concurrency() -> None:
    compactor = ChatAutoCompactor.build(list_running_chat_agent_names=lambda: [])
    assert compactor._max_concurrency == _DEFAULT_CHECK_CONCURRENCY
    assert _DEFAULT_CHECK_CONCURRENCY == 4


def test_check_agent_success() -> None:
    recorded_commands: list[list[str]] = []
    recorded_kwargs: dict[str, object] = {}

    def fake_runner(command: Sequence[str], **kwargs: object) -> FinishedProcess:
        recorded_commands.append(list(command))
        recorded_kwargs.update(kwargs)
        return _make_finished_process(
            command=command, returncode=0, stdout="No agents require compaction."
        )

    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: ["chat-1"],
        runner=fake_runner,
        mngr_binary="mngr-custom",
    )
    result = compactor.check_agent("chat-1")

    assert result is not None
    assert result.returncode == 0
    assert recorded_commands == [["mngr-custom", "autocompact", "run", "chat-1"]]
    assert recorded_kwargs.get("is_checked") is False


def test_check_agent_exit_code_1_logged_as_debug(loguru_records: list[str]) -> None:
    def fake_runner(
        command: Sequence[str], is_checked: bool = False, **kwargs: object
    ) -> FinishedProcess:
        return _make_finished_process(
            command=command,
            returncode=1,
            stderr="Agent 'chat-1' does not support context compaction",
        )

    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: ["chat-1"],
        runner=fake_runner,
    )
    result = compactor.check_agent("chat-1")

    assert result is None
    debug_logs = [log for log in loguru_records if log.startswith("DEBUG") and "chat-1" in log]
    assert len(debug_logs) == 1
    assert "does not support context compaction" in debug_logs[0]
    warning_logs = [log for log in loguru_records if log.startswith("WARNING")]
    assert len(warning_logs) == 0


def test_check_agent_other_nonzero_exit_logged_as_warning(loguru_records: list[str]) -> None:
    def fake_runner(
        command: Sequence[str], is_checked: bool = False, **kwargs: object
    ) -> FinishedProcess:
        return _make_finished_process(
            command=command,
            returncode=2,
            stderr="invalid syntax",
        )

    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: ["chat-1"],
        runner=fake_runner,
    )
    result = compactor.check_agent("chat-1")

    assert result is None
    warning_logs = [log for log in loguru_records if log.startswith("WARNING") and "chat-1" in log]
    assert len(warning_logs) == 1
    assert "return code 2" in warning_logs[0]


def test_check_agent_process_setup_error_handled_gracefully(loguru_records: list[str]) -> None:
    def fake_runner(command: Sequence[str], **kwargs: object) -> FinishedProcess:
        raise ProcessSetupError(
            command=tuple(command),
            stdout="",
            stderr="mngr executable not found",
            is_output_already_logged=False,
        )

    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: ["chat-1"],
        runner=fake_runner,
    )
    result = compactor.check_agent("chat-1")

    assert result is None
    warning_logs = [log for log in loguru_records if log.startswith("WARNING") and "chat-1" in log]
    assert len(warning_logs) == 1


def test_sweep_checks_all_running_chat_agents() -> None:
    recorded_commands: list[list[str]] = []
    lock = threading.Lock()

    def fake_runner(command: Sequence[str], **kwargs: object) -> FinishedProcess:
        with lock:
            recorded_commands.append(list(command))
        return _make_finished_process(command=command, returncode=0)

    running_chats = ["chat-alpha", "chat-beta", "chat-gamma"]
    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: running_chats,
        runner=fake_runner,
        mngr_binary="mngr",
    )
    results = compactor.sweep()

    assert len(results) == 3
    assert sorted(recorded_commands) == [
        ["mngr", "autocompact", "run", "chat-alpha"],
        ["mngr", "autocompact", "run", "chat-beta"],
        ["mngr", "autocompact", "run", "chat-gamma"],
    ]


def test_sweep_runs_concurrently() -> None:
    barrier = threading.Barrier(3)

    def fake_runner(command: Sequence[str], **kwargs: object) -> FinishedProcess:
        barrier.wait(timeout=2.0)
        return _make_finished_process(command=command, returncode=0)

    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: ["chat-1", "chat-2", "chat-3"],
        runner=fake_runner,
        max_concurrency=3,
    )
    results = compactor.sweep()
    assert len(results) == 3


def test_sweep_stops_early_if_stop_event_set() -> None:
    recorded_commands: list[list[str]] = []

    compactor: ChatAutoCompactor

    def fake_runner(command: Sequence[str], **kwargs: object) -> FinishedProcess:
        recorded_commands.append(list(command))
        compactor._stop_event.set()
        return _make_finished_process(command=command, returncode=0)

    running_chats = ["chat-1", "chat-2", "chat-3"]
    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: running_chats,
        runner=fake_runner,
        max_concurrency=1,
    )
    results = compactor.sweep()

    assert len(results) == 1
    assert recorded_commands == [["mngr", "autocompact", "run", "chat-1"]]


def test_start_and_stop_lifecycle() -> None:
    sweep_called = threading.Event()

    def fake_runner(command: Sequence[str], **kwargs: object) -> FinishedProcess:
        sweep_called.set()
        return _make_finished_process(command=command, returncode=0)

    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: ["test-chat"],
        runner=fake_runner,
        interval_seconds=0.01,
    )
    compactor.start()
    assert compactor._thread is not None
    assert compactor._thread.is_alive()

    compactor.start()

    poll_until(sweep_called.is_set, timeout=2.0)
    assert sweep_called.is_set()

    compactor.stop()
    assert compactor._thread is None
