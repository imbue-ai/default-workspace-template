import threading
from pathlib import Path

from imbue.chat.harnesses.model_state_poll import AgentStatePoller
from imbue.chat.harnesses.model_state_poll import WatchedPathPurpose

_MODEL = WatchedPathPurpose.MODEL_STATE
_TASKS = WatchedPathPurpose.BACKGROUND_TASKS


def _build_poller_over(
    paths_by_agent: dict[str, dict[WatchedPathPurpose, Path]],
    changed: list[tuple[str, WatchedPathPurpose]],
    passes: list[int] | None = None,
) -> AgentStatePoller:
    return AgentStatePoller.build(
        list_watched_paths=lambda: {agent_id: dict(paths) for agent_id, paths in paths_by_agent.items()},
        on_path_changed=lambda agent_id, purpose: changed.append((agent_id, purpose)),
        on_pass_complete=None if passes is None else lambda: passes.append(len(changed)),
    )


def test_poll_once_fires_on_first_sighting_then_stays_quiet_while_unchanged(tmp_path: Path) -> None:
    state_path = tmp_path / "model_state.json"
    state_path.write_text('{"model": "opus"}')
    changed: list[tuple[str, WatchedPathPurpose]] = []
    poller = _build_poller_over({"agent-1": {_MODEL: state_path}}, changed)

    # The first pass has nothing remembered, so the agent counts as changed once.
    poller.poll_once()
    assert changed == [("agent-1", _MODEL)]

    # Unchanged file -> no further callbacks, however many passes run.
    poller.poll_once()
    poller.poll_once()
    assert changed == [("agent-1", _MODEL)]


def test_poll_once_fires_on_content_change_and_on_deletion(tmp_path: Path) -> None:
    state_path = tmp_path / "model_state.json"
    state_path.write_text('{"model": "opus"}')
    changed: list[tuple[str, WatchedPathPurpose]] = []
    poller = _build_poller_over({"agent-1": {_MODEL: state_path}}, changed)
    poller.poll_once()

    # A rewrite with different content changes the stamp.
    state_path.write_text('{"model": "sonnet-4.5"}')
    poller.poll_once()
    assert len(changed) == 2

    # Deletion flips the stamp to absent, which is a change too.
    state_path.unlink()
    poller.poll_once()
    assert len(changed) == 3

    # Still absent -> quiet.
    poller.poll_once()
    assert len(changed) == 3


def test_poll_once_fires_when_a_missing_file_appears(tmp_path: Path) -> None:
    state_path = tmp_path / "model_state.json"
    changed: list[tuple[str, WatchedPathPurpose]] = []
    poller = _build_poller_over({"agent-1": {_MODEL: state_path}}, changed)

    # First sighting of an absent file is one callback (the recompute no-ops), then quiet.
    poller.poll_once()
    poller.poll_once()
    assert len(changed) == 1

    state_path.write_text('{"model": "opus"}')
    poller.poll_once()
    assert len(changed) == 2


def test_an_agents_watched_paths_are_tracked_apart(tmp_path: Path) -> None:
    """A model-state rewrite fires only the model purpose, and a marker added to the chat's directory only the
    tasks purpose: the directory's mtime moves on the add, which is the change the poller sees."""
    state_path = tmp_path / "model_state.json"
    state_path.write_text('{"model": "opus"}')
    marker_dir = tmp_path / "background_tasks" / "agent-1"
    marker_dir.mkdir(parents=True)
    changed: list[tuple[str, WatchedPathPurpose]] = []
    poller = _build_poller_over({"agent-1": {_MODEL: state_path, _TASKS: marker_dir}}, changed)
    poller.poll_once()
    assert sorted(changed) == sorted([("agent-1", _MODEL), ("agent-1", _TASKS)])
    changed.clear()

    state_path.write_text('{"model": "sonnet-4.5"}')
    poller.poll_once()
    assert changed == [("agent-1", _MODEL)]
    changed.clear()

    (marker_dir / "run_in_background-1.json").write_text("{}")
    poller.poll_once()
    assert changed == [("agent-1", _TASKS)]
    changed.clear()

    (marker_dir / "run_in_background-1.json").unlink()
    poller.poll_once()
    assert changed == [("agent-1", _TASKS)]


def test_a_purpose_no_longer_listed_is_forgotten_while_the_others_stay(tmp_path: Path) -> None:
    """An agent that stops being its chat's active agent drops its tasks purpose; listed again, it is re-read."""
    state_path = tmp_path / "model_state.json"
    state_path.write_text("{}")
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    paths_by_agent = {"agent-1": {_MODEL: state_path, _TASKS: marker_dir}}
    changed: list[tuple[str, WatchedPathPurpose]] = []
    poller = _build_poller_over(paths_by_agent, changed)
    poller.poll_once()
    changed.clear()

    del paths_by_agent["agent-1"][_TASKS]
    poller.poll_once()
    assert changed == []

    paths_by_agent["agent-1"][_TASKS] = marker_dir
    poller.poll_once()
    assert changed == [("agent-1", _TASKS)]


def test_every_pass_ends_with_the_pass_complete_callback(tmp_path: Path) -> None:
    """What no stamp shows (a marker's process dying) is rechecked after every pass, changed or not."""
    state_path = tmp_path / "model_state.json"
    state_path.write_text("{}")
    changed: list[tuple[str, WatchedPathPurpose]] = []
    passes: list[int] = []
    poller = _build_poller_over({"agent-1": {_MODEL: state_path}}, changed, passes)

    poller.poll_once()
    poller.poll_once()

    # Called after the pass's own callbacks: the first saw the first sighting, the second nothing new.
    assert passes == [1, 1]


def test_delisted_agent_is_forgotten_so_a_relisted_one_is_rederived(tmp_path: Path) -> None:
    state_path = tmp_path / "model_state.json"
    state_path.write_text('{"model": "opus"}')
    paths_by_agent = {"agent-1": {_MODEL: state_path}}
    changed: list[tuple[str, WatchedPathPurpose]] = []
    poller = _build_poller_over(paths_by_agent, changed)
    poller.poll_once()
    assert len(changed) == 1

    # The agent leaves the listing (destroyed); its stamp must not linger.
    del paths_by_agent["agent-1"]
    poller.poll_once()
    assert len(changed) == 1

    # Relisted with the identical file: it is re-derived rather than assumed unchanged.
    paths_by_agent["agent-1"] = {_MODEL: state_path}
    poller.poll_once()
    assert len(changed) == 2


def test_poller_uses_one_thread_regardless_of_agent_count(tmp_path: Path) -> None:
    paths_by_agent = {f"agent-{i}": {_MODEL: tmp_path / f"agent-{i}" / "model_state.json"} for i in range(20)}
    for paths in paths_by_agent.values():
        paths[_MODEL].parent.mkdir(parents=True)
        paths[_MODEL].write_text("{}")
    changed: list[tuple[str, WatchedPathPurpose]] = []
    poller = _build_poller_over(paths_by_agent, changed)

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
