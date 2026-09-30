import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from imbue.chat.autocompact import ChatAutoCompactor
from imbue.chat.autocompact import is_proactive_autocompact_enabled
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.utils.polling import poll_until


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


def test_check_agent_success() -> None:
    recorded_commands: list[list[str]] = []
    recorded_kwargs: dict[str, object] = {}

    def fake_runner(command: Sequence[str], **kwargs: object) -> FinishedProcess:
        recorded_commands.append(list(command))
        recorded_kwargs.update(kwargs)
        return _make_finished_process(command=command, returncode=0, stdout="No agents require compaction.")

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
    def fake_runner(command: Sequence[str], is_checked: bool = False, **kwargs: object) -> FinishedProcess:
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
    def fake_runner(command: Sequence[str], is_checked: bool = False, **kwargs: object) -> FinishedProcess:
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


def test_sweep_launches_nothing_while_autocompact_is_disabled() -> None:
    """With the mode off every `mngr autocompact run` is a no-op, so the sweep must not pay for one."""
    recorded_commands: list[list[str]] = []

    def fake_runner(command: Sequence[str], **kwargs: object) -> FinishedProcess:
        recorded_commands.append(list(command))
        return _make_finished_process(command=command, returncode=0)

    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: ["chat-alpha", "chat-beta"],
        is_enabled=lambda: False,
        runner=fake_runner,
    )

    assert compactor.sweep() == []
    assert recorded_commands == []


def test_sweep_checks_every_running_chat_in_one_command() -> None:
    recorded_commands: list[list[str]] = []

    def fake_runner(command: Sequence[str], **kwargs: object) -> FinishedProcess:
        recorded_commands.append(list(command))
        return _make_finished_process(command=command, returncode=0)

    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: ["chat-alpha", "chat-beta", "chat-gamma"],
        is_enabled=lambda: True,
        runner=fake_runner,
    )
    results = compactor.sweep()

    assert len(results) == 1
    assert recorded_commands == [["mngr", "autocompact", "run", "chat-alpha", "chat-beta", "chat-gamma"]]


def test_an_unreadable_mode_still_runs_the_sweep(loguru_records: list[str]) -> None:
    """An unreadable config must cost a launch, not silently turn compaction off."""
    recorded_commands: list[list[str]] = []

    def fake_runner(command: Sequence[str], **kwargs: object) -> FinishedProcess:
        recorded_commands.append(list(command))
        return _make_finished_process(command=command, returncode=0)

    def unreadable_mode() -> bool:
        raise ConfigParseError("Invalid config for 'plugins.autocompact'")

    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: ["chat-alpha", "chat-beta"],
        is_enabled=unreadable_mode,
        runner=fake_runner,
    )
    compactor.sweep()

    assert recorded_commands == [["mngr", "autocompact", "run", "chat-alpha", "chat-beta"]]
    warning_logs = [log for log in loguru_records if log.startswith("WARNING") and "autocompact mode" in log]
    assert len(warning_logs) == 1


def test_a_failed_batch_is_retried_one_chat_at_a_time() -> None:
    """One chat the batch cannot resolve (stopped since it was listed) fails the whole command;
    the others must still be checked."""
    recorded_commands: list[list[str]] = []

    def fake_runner(command: Sequence[str], **kwargs: object) -> FinishedProcess:
        recorded_commands.append(list(command))
        is_stopped_chat_named = "chat-stopped" in command
        return _make_finished_process(command=command, returncode=1 if is_stopped_chat_named else 0)

    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: ["chat-alpha", "chat-stopped", "chat-gamma"],
        is_enabled=lambda: True,
        runner=fake_runner,
    )
    results = compactor.sweep()

    assert recorded_commands == [
        ["mngr", "autocompact", "run", "chat-alpha", "chat-stopped", "chat-gamma"],
        ["mngr", "autocompact", "run", "chat-alpha"],
        ["mngr", "autocompact", "run", "chat-stopped"],
        ["mngr", "autocompact", "run", "chat-gamma"],
    ]
    assert [result is not None for result in results] == [True, False, True]


@pytest.mark.parametrize(
    "batch_outcome",
    ["crash", "timeout", "cannot_launch"],
)
def test_a_batch_failure_no_single_chat_causes_is_not_retried_per_chat(batch_outcome: str) -> None:
    """A crash, a timeout, or no mngr to launch would fail again for each chat, so only one launch is paid."""
    recorded_commands: list[list[str]] = []

    def fake_runner(command: Sequence[str], **kwargs: object) -> FinishedProcess:
        recorded_commands.append(list(command))
        if batch_outcome == "cannot_launch":
            raise ProcessSetupError(
                command=tuple(command), stdout="", stderr="mngr not found", is_output_already_logged=False
            )
        return FinishedProcess(
            command=tuple(command),
            returncode=2 if batch_outcome == "crash" else 1,
            stdout="",
            stderr="",
            is_timed_out=batch_outcome == "timeout",
            is_output_already_logged=False,
        )

    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: ["chat-alpha", "chat-beta", "chat-gamma"],
        is_enabled=lambda: True,
        runner=fake_runner,
    )

    assert compactor.sweep() == [None]
    assert recorded_commands == [["mngr", "autocompact", "run", "chat-alpha", "chat-beta", "chat-gamma"]]


def test_the_one_chat_retry_stops_early_if_stop_event_set() -> None:
    recorded_commands: list[list[str]] = []

    compactor: ChatAutoCompactor

    def fake_runner(command: Sequence[str], **kwargs: object) -> FinishedProcess:
        recorded_commands.append(list(command))
        if len(recorded_commands) == 2:
            compactor._stop_event.set()
        return _make_finished_process(command=command, returncode=1)

    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: ["chat-1", "chat-2", "chat-3"],
        is_enabled=lambda: True,
        runner=fake_runner,
    )
    compactor.sweep()

    assert recorded_commands == [
        ["mngr", "autocompact", "run", "chat-1", "chat-2", "chat-3"],
        ["mngr", "autocompact", "run", "chat-1"],
    ]


@pytest.mark.parametrize(
    ("settings", "is_enabled"),
    [
        ("", False),
        ('[plugins.autocompact]\nmode = "on_next_prompt"\n', False),
        ('[plugins.autocompact]\nmode = "proactive_timer"\n', True),
    ],
)
def test_the_mode_is_read_the_way_the_workspace_mngr_reads_it(
    settings: str, is_enabled: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loaded through mngr's own loader, so the sweep runs exactly when `mngr autocompact run` would act."""
    config_dir = tmp_path / ".mngr"
    config_dir.mkdir()
    (config_dir / "settings.toml").write_text("is_allowed_in_pytest = true\n" + settings)
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))

    assert is_proactive_autocompact_enabled() is is_enabled


def test_start_and_stop_lifecycle() -> None:
    sweep_called = threading.Event()

    def fake_runner(command: Sequence[str], **kwargs: object) -> FinishedProcess:
        sweep_called.set()
        return _make_finished_process(command=command, returncode=0)

    compactor = ChatAutoCompactor.build(
        list_running_chat_agent_names=lambda: ["test-chat"],
        is_enabled=lambda: True,
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
