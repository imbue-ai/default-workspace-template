import threading
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path

import pytest

from imbue.chat.autocompact import ChatAutoCompactor
from imbue.chat.autocompact import SLOW_SWEEP_WARNING_SECONDS
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import MngrError
from imbue.mngr.utils.polling import poll_until


def _recording_compact(
    compacted_of: Callable[[Sequence[str]], Sequence[str]] = lambda names: [],
) -> tuple[list[list[str]], Callable[[Sequence[str]], Sequence[str]]]:
    """A fake plugin call recording each batch of names it is given, answering it with ``compacted_of(names)``."""
    recorded_batches: list[list[str]] = []

    def compact(names: Sequence[str]) -> Sequence[str]:
        recorded_batches.append(list(names))
        return compacted_of(names)

    return recorded_batches, compact


def test_a_sweep_with_no_opted_in_chat_makes_no_call() -> None:
    """Nothing opted in (or nothing running) must cost nothing: not even the config read the call makes."""
    recorded_batches, compact = _recording_compact()

    compactor = ChatAutoCompactor.build(list_opted_in_chat_agent_names=list, compact=compact)

    assert compactor.sweep() == []
    assert recorded_batches == []


def test_a_sweep_passes_every_opted_in_chat_to_one_plugin_call() -> None:
    recorded_batches, compact = _recording_compact(lambda names: ["chat-beta"])

    compactor = ChatAutoCompactor.build(
        list_opted_in_chat_agent_names=lambda: ["chat-alpha", "chat-beta", "chat-gamma"],
        compact=compact,
    )

    assert compactor.sweep() == ["chat-beta"]
    assert recorded_batches == [["chat-alpha", "chat-beta", "chat-gamma"]]


def test_each_compacted_agent_is_reported_once_and_logged(loguru_records: list[str]) -> None:
    reported: list[str] = []
    _recorded_batches, compact = _recording_compact(lambda names: ["chat-alpha", "chat-gamma"])

    compactor = ChatAutoCompactor.build(
        list_opted_in_chat_agent_names=lambda: ["chat-alpha", "chat-beta", "chat-gamma"],
        compact=compact,
        on_compaction_requested=reported.append,
        harness_of_agent=lambda name: "claude" if name == "chat-alpha" else None,
    )
    compactor.sweep()

    assert reported == ["chat-alpha", "chat-gamma"]
    requested_logs = [record for record in loguru_records if "autocompact: requested" in record]
    assert requested_logs == [
        "INFO autocompact: requested agent=chat-alpha harness=claude",
        "INFO autocompact: requested agent=chat-gamma",
    ]


@pytest.mark.parametrize(
    "compact_error",
    [
        MngrError("host offline"),
        OSError("disk gone"),
        ConfigParseError("Invalid config for 'plugins.autocompact'"),
        PermissionError("settings.toml"),
    ],
)
def test_a_failed_plugin_call_logs_and_reports_nothing(compact_error: Exception, loguru_records: list[str]) -> None:
    reported: list[str] = []

    def failing_compact(names: Sequence[str]) -> Sequence[str]:
        raise compact_error

    compactor = ChatAutoCompactor.build(
        list_opted_in_chat_agent_names=lambda: ["chat-alpha", "chat-beta"],
        compact=failing_compact,
        on_compaction_requested=reported.append,
    )

    assert compactor.sweep() == []
    assert reported == []
    warning_logs = [log for log in loguru_records if log.startswith("WARNING") and "chat-alpha, chat-beta" in log]
    assert len(warning_logs) == 1


def test_an_unexpected_error_in_one_tick_is_logged_and_the_next_tick_still_runs(loguru_records: list[str]) -> None:
    second_tick_ran = threading.Event()
    calls: list[None] = []

    def compact(names: Sequence[str]) -> Sequence[str]:
        calls.append(None)
        if len(calls) == 1:
            raise RuntimeError("corrupt agent record")
        second_tick_ran.set()
        return []

    compactor = ChatAutoCompactor.build(
        list_opted_in_chat_agent_names=lambda: ["chat-alpha"],
        compact=compact,
        interval_seconds=0.01,
    )
    compactor.start()
    try:
        poll_until(second_tick_ran.is_set, timeout=5.0)
    finally:
        compactor.stop()

    error_logs = [log for log in loguru_records if log.startswith("ERROR") and "sweep failed unexpectedly" in log]
    assert len(error_logs) == 1


def test_a_slow_sweep_logs_a_warning(loguru_records: list[str]) -> None:
    clock = iter([100.0, 100.0 + SLOW_SWEEP_WARNING_SECONDS + 1.0])

    compactor = ChatAutoCompactor.build(
        list_opted_in_chat_agent_names=lambda: ["chat-alpha"],
        compact=lambda names: [],
        monotonic=lambda: next(clock),
    )
    compactor.sweep()

    warning_logs = [log for log in loguru_records if log.startswith("WARNING") and "slow sweep" in log]
    assert len(warning_logs) == 1


def test_a_quick_sweep_logs_no_warning(loguru_records: list[str]) -> None:
    clock = iter([100.0, 101.0])

    compactor = ChatAutoCompactor.build(
        list_opted_in_chat_agent_names=lambda: ["chat-alpha"],
        compact=lambda names: [],
        monotonic=lambda: next(clock),
    )
    compactor.sweep()

    assert [log for log in loguru_records if log.startswith("WARNING")] == []


def test_an_invalid_mode_in_the_settings_file_logs_and_requests_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loguru_records: list[str]
) -> None:
    """mngr's loader rejects an unknown mode with a pydantic error, which must not end the sweep thread."""
    _use_mngr_settings('[plugins.autocompact]\nmode = "bogus"\n', tmp_path, monkeypatch)

    compactor = ChatAutoCompactor.build(list_opted_in_chat_agent_names=lambda: ["chat-alpha"])

    assert compactor.sweep() == []
    warning_logs = [log for log in loguru_records if log.startswith("WARNING") and "chat-alpha" in log]
    assert len(warning_logs) == 1


def _use_mngr_settings(settings: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / ".mngr"
    config_dir.mkdir()
    (config_dir / "settings.toml").write_text("is_allowed_in_pytest = true\n" + settings)
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))


def test_start_and_stop_lifecycle() -> None:
    sweep_called = threading.Event()

    def compact(names: Sequence[str]) -> Sequence[str]:
        sweep_called.set()
        return []

    compactor = ChatAutoCompactor.build(
        list_opted_in_chat_agent_names=lambda: ["test-chat"],
        compact=compact,
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
