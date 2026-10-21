import os
import threading
from pathlib import Path

from imbue.chat.harnesses.agent_file_poll import AgentFilePoller

_THREAD_NAME = "agent-file-poll-test-4417"


def _build_poller_over(path_by_agent: dict[str, Path], changed_agent_ids: list[str]) -> AgentFilePoller:
    return AgentFilePoller.build(
        list_paths=lambda: dict(path_by_agent),
        on_file_changed=changed_agent_ids.append,
        thread_name=_THREAD_NAME,
    )


def test_poll_once_fires_on_first_sighting_then_stays_quiet_while_unchanged(tmp_path: Path) -> None:
    state_path = tmp_path / "model_state.json"
    state_path.write_text('{"model": "opus"}')
    changed: list[str] = []
    poller = _build_poller_over({"agent-1": state_path}, changed)

    # The first pass has nothing remembered, so the agent counts as changed once.
    poller.poll_once()
    assert changed == ["agent-1"]

    # Unchanged file -> no further callbacks, however many passes run.
    poller.poll_once()
    poller.poll_once()
    assert changed == ["agent-1"]


def test_poll_once_fires_on_content_change_and_on_deletion(tmp_path: Path) -> None:
    state_path = tmp_path / "model_state.json"
    state_path.write_text('{"model": "opus"}')
    changed: list[str] = []
    poller = _build_poller_over({"agent-1": state_path}, changed)
    poller.poll_once()

    # A rewrite with different content changes the stamp.
    state_path.write_text('{"model": "sonnet-4.5"}')
    poller.poll_once()
    assert changed == ["agent-1", "agent-1"]

    # Deletion flips the stamp to absent, which is a change too.
    state_path.unlink()
    poller.poll_once()
    assert changed == ["agent-1", "agent-1", "agent-1"]

    # Still absent -> quiet.
    poller.poll_once()
    assert changed == ["agent-1", "agent-1", "agent-1"]


def test_poll_once_fires_when_a_missing_file_appears(tmp_path: Path) -> None:
    state_path = tmp_path / "model_state.json"
    changed: list[str] = []
    poller = _build_poller_over({"agent-1": state_path}, changed)

    # First sighting of an absent file is one callback (the recompute no-ops), then quiet.
    poller.poll_once()
    poller.poll_once()
    assert changed == ["agent-1"]

    state_path.write_text('{"model": "opus"}')
    poller.poll_once()
    assert changed == ["agent-1", "agent-1"]


def test_delisted_agent_is_forgotten_so_a_relisted_one_is_rederived(tmp_path: Path) -> None:
    state_path = tmp_path / "model_state.json"
    state_path.write_text('{"model": "opus"}')
    path_by_agent = {"agent-1": state_path}
    changed: list[str] = []
    poller = _build_poller_over(path_by_agent, changed)
    poller.poll_once()
    assert changed == ["agent-1"]

    # The agent leaves the listing (destroyed); its stamp must not linger.
    del path_by_agent["agent-1"]
    poller.poll_once()
    assert changed == ["agent-1"]

    # Relisted with the identical file: it is re-derived rather than assumed unchanged.
    path_by_agent["agent-1"] = state_path
    poller.poll_once()
    assert changed == ["agent-1", "agent-1"]


class _FailingOnceCallbackError(Exception):
    """The error the test callback raises on its first call."""


def test_a_callback_that_raises_neither_stops_the_pass_nor_later_polls(
    tmp_path: Path, loguru_records: list[str]
) -> None:
    failing_path = tmp_path / "failing" / "model_state.json"
    other_path = tmp_path / "other" / "model_state.json"
    for path in (failing_path, other_path):
        path.parent.mkdir(parents=True)
        path.write_text("{}")
    changed: list[str] = []

    def record_and_fail_the_first_time(agent_id: str) -> None:
        changed.append(agent_id)
        if changed == ["agent-failing"]:
            raise _FailingOnceCallbackError("recompute failed")

    poller = AgentFilePoller.build(
        list_paths=lambda: {"agent-failing": failing_path, "agent-other": other_path},
        on_file_changed=record_and_fail_the_first_time,
        thread_name=_THREAD_NAME,
    )

    poller.poll_once()
    assert changed == ["agent-failing", "agent-other"]
    assert [record for record in loguru_records if record.startswith("ERROR ")] == [
        f"ERROR {_THREAD_NAME}: the change callback failed for agent agent-failing"
    ]

    failing_path.write_text('{"model": "opus"}')
    poller.poll_once()
    assert changed == ["agent-failing", "agent-other", "agent-failing"]


def test_poller_uses_one_thread_regardless_of_agent_count(tmp_path: Path) -> None:
    path_by_agent = {f"agent-{i}": tmp_path / f"agent-{i}" / "model_state.json" for i in range(20)}
    for path in path_by_agent.values():
        path.parent.mkdir(parents=True)
        path.write_text("{}")
    changed: list[str] = []
    poller = _build_poller_over(path_by_agent, changed)

    threads_before = set(threading.enumerate())
    poller.start()
    new_threads = set(threading.enumerate()) - threads_before
    try:
        assert len(new_threads) == 1
        assert next(iter(new_threads)).name == _THREAD_NAME
    finally:
        poller.stop()
    # The join in stop() has completed, so the poller thread is gone again.
    assert set(threading.enumerate()) - threads_before == set()


def test_stop_is_idempotent_and_safe_without_start(tmp_path: Path) -> None:
    poller = _build_poller_over({}, [])
    poller.stop()
    poller.start()
    poller.stop()
    poller.stop()


def test_every_pass_ends_with_the_pass_complete_callback(tmp_path: Path) -> None:
    """What no stamp shows (a marker's process dying) is rechecked after every pass, changed or not."""
    state_path = tmp_path / "model_state.json"
    state_path.write_text("{}")
    changed: list[str] = []
    passes: list[int] = []
    poller = AgentFilePoller.build(
        list_paths=lambda: {"agent-1": state_path},
        on_file_changed=changed.append,
        thread_name=_THREAD_NAME,
        on_pass_complete=lambda: passes.append(len(changed)),
    )

    poller.poll_once()
    poller.poll_once()

    # Called after the pass's own callbacks: the first saw the first sighting, the second nothing new.
    assert passes == [1, 1]


def test_start_has_read_every_path_before_it_returns(tmp_path: Path) -> None:
    """The app starts answering as soon as its manager starts, so a chat already waiting on a task must not read
    idle for the poller's first interval."""
    markers = tmp_path / "background_tasks"
    markers.mkdir()
    (markers / "run_in_background-a.json").write_text("{}")
    changed: list[str] = []
    poller = _build_poller_over({"agent-1": markers}, changed)

    poller.start()
    try:
        assert changed == ["agent-1"]
    finally:
        poller.stop()


def test_a_marker_renamed_within_one_mtime_tick_still_fires(tmp_path: Path) -> None:
    """A directory's mtime is too coarse to tell two changes in one tick apart and its size need not move, so a
    marker swapped for another between two passes is seen by the directory's entry names."""
    markers = tmp_path / "background_tasks"
    markers.mkdir()
    (markers / "claude-a.json").write_text("{}")
    changed: list[str] = []
    poller = _build_poller_over({"agent-1": markers}, changed)
    poller.poll_once()
    stamp = markers.stat()

    (markers / "claude-a.json").rename(markers / "claude-b.json")
    os.utime(markers, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    poller.poll_once()

    assert changed == ["agent-1", "agent-1"]
