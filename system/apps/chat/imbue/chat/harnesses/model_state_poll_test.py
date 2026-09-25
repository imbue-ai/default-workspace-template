import threading
from pathlib import Path

from imbue.chat.harnesses.model_state_poll import ModelStatePoller


def _build_poller_over(
    path_by_agent: dict[str, Path], changed_agent_ids: list[str]
) -> ModelStatePoller:
    return ModelStatePoller.build(
        list_model_state_paths=lambda: dict(path_by_agent),
        on_model_state_changed=changed_agent_ids.append,
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
        assert next(iter(new_threads)).name == "model-state-poll"
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
