import threading
from collections.abc import Hashable
from pathlib import Path

from imbue.chat.agent_state_poll import AgentStatePoller
from imbue.chat.agent_state_poll import AgentStateWatch
from imbue.chat.agent_state_poll import read_file_stamp


def _file_watch(path_by_agent: dict[str, Path], changed_agent_ids: list[str]) -> AgentStateWatch:
    return AgentStateWatch(
        read_stamp_by_agent=lambda: {agent_id: read_file_stamp(path) for agent_id, path in path_by_agent.items()},
        on_changed=changed_agent_ids.append,
    )


def _build_poller_over(path_by_agent: dict[str, Path], changed_agent_ids: list[str]) -> AgentStatePoller:
    return AgentStatePoller.build(watches=(_file_watch(path_by_agent, changed_agent_ids),))


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


def test_each_watch_fires_only_its_own_callback(tmp_path: Path) -> None:
    model_state_path = tmp_path / "model_state.json"
    model_state_path.write_text("{}")
    liveness_by_agent: dict[str, Hashable] = {"agent-1": True}
    model_changes: list[str] = []
    liveness_changes: list[str] = []
    poller = AgentStatePoller.build(
        watches=(
            _file_watch({"agent-1": model_state_path}, model_changes),
            AgentStateWatch(read_stamp_by_agent=lambda: dict(liveness_by_agent), on_changed=liveness_changes.append),
        )
    )
    poller.poll_once()
    assert (model_changes, liveness_changes) == (["agent-1"], ["agent-1"])

    # A stamp that is not a file's (here, whether a process lives) changes on its own.
    liveness_by_agent["agent-1"] = False
    poller.poll_once()
    assert (model_changes, liveness_changes) == (["agent-1"], ["agent-1", "agent-1"])


def test_poller_uses_one_thread_regardless_of_agent_count(tmp_path: Path) -> None:
    path_by_agent = {f"agent-{i}": tmp_path / f"agent-{i}" / "model_state.json" for i in range(20)}
    for path in path_by_agent.values():
        path.parent.mkdir(parents=True)
        path.write_text("{}")
    changed: list[str] = []
    poller = AgentStatePoller.build(
        watches=(_file_watch(path_by_agent, changed), _file_watch(path_by_agent, changed))
    )

    threads_before = set(threading.enumerate())
    poller.start()
    new_threads = set(threading.enumerate()) - threads_before
    try:
        assert len(new_threads) == 1
        assert next(iter(new_threads)).name == "agent-state-poll"
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
