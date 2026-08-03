"""Tests for following another process's ``mngr observe`` event stream.

Every event these tests write goes through mngr's own ``append_observe_event``
and event constructors, and the "live observer" is modelled by holding mngr's
own ``acquire_observe_lock`` -- so the follower is exercised against the real
file format and the real lock, not a hand-rolled imitation of either.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.api.observe import acquire_observe_lock
from imbue.mngr.api.observe import append_observe_event
from imbue.mngr.api.observe import get_observe_events_path
from imbue.mngr.api.observe import make_agent_removed_event
from imbue.mngr.api.observe import make_agent_state_event
from imbue.mngr.api.observe import make_full_agent_state_event
from imbue.mngr.api.observe import release_observe_lock
from imbue.mngr.utils.polling import poll_until
from imbue.system_interface.agent_events import ObserveEventFollower
from imbue.system_interface.agent_events import ObserveStreamUnavailableError
from imbue.system_interface.agent_events import find_last_full_state_offset
from imbue.system_interface.agent_events import is_observe_writer_running
from imbue.system_interface.testing import build_agent_details


@contextmanager
def _running_observer(events_base_dir: Path) -> Iterator[None]:
    """Hold the observe lock for the block, exactly as a live ``mngr observe`` does.

    ``flock`` is scoped to an open file description rather than a process, so a
    lock taken here is genuinely exclusive against the follower's probe even
    though both live in this process -- modelling a live writer needs no
    subprocess.
    """
    lock_fd = acquire_observe_lock(events_base_dir)
    try:
        yield
    finally:
        release_observe_lock(lock_fd)


def _event_types(lines: list[str]) -> list[str]:
    return [json.loads(line)["type"] for line in lines]


def _snapshot_agent_names(line: str) -> list[str]:
    payload: dict[str, Any] = json.loads(line)
    return [agent["name"] for agent in payload["agents"]]


def _serialized_event_line(event: Any) -> str:
    """Render one event exactly as ``append_observe_event`` writes it."""
    return json.dumps(event.model_dump(mode="json"), separators=(",", ":")) + "\n"


def test_a_held_observe_lock_is_what_makes_a_stream_followable(tmp_path: Path) -> None:
    """The lock -- not the presence of the file -- is the liveness signal."""
    assert is_observe_writer_running(tmp_path) is False
    with _running_observer(tmp_path):
        assert is_observe_writer_running(tmp_path) is True
    # The lock file now exists but is unheld, which must still read as "no writer"
    # rather than as "an observer once ran here".
    assert is_observe_writer_running(tmp_path) is False


def test_follower_refuses_to_start_when_no_observer_is_running(tmp_path: Path) -> None:
    """Tailing a file nobody is appending to is the exact silent freeze to avoid."""
    lines: list[str] = []
    follower = ObserveEventFollower(events_base_dir=tmp_path, on_line=lines.append)
    with pytest.raises(ObserveStreamUnavailableError):
        follower.start()


def test_follower_starts_folding_at_the_newest_snapshot(tmp_path: Path) -> None:
    """Replay begins at the last full snapshot, not at the beginning of history.

    Folding a mid-stream per-agent update with nothing behind it would collapse
    the consumer's whole agent set down to that one agent, so history before the
    newest snapshot must not be replayed.
    """
    superseded = build_agent_details("superseded-agent")
    current = build_agent_details("current-agent")
    append_observe_event(tmp_path, make_full_agent_state_event([superseded]))
    append_observe_event(tmp_path, make_agent_state_event(superseded))
    append_observe_event(tmp_path, make_full_agent_state_event([current]))
    append_observe_event(tmp_path, make_agent_state_event(current))

    lines: list[str] = []
    follower = ObserveEventFollower(events_base_dir=tmp_path, on_line=lines.append)
    with _running_observer(tmp_path):
        follower.poll_once()

    assert _event_types(lines) == ["AGENTS_FULL_STATE", "AGENT_STATE"]
    assert _snapshot_agent_names(lines[0]) == ["current-agent"]


def test_find_last_full_state_offset_is_none_before_any_snapshot(tmp_path: Path) -> None:
    agent = build_agent_details("only")
    append_observe_event(tmp_path, make_agent_state_event(agent))
    assert find_last_full_state_offset(get_observe_events_path(tmp_path)) is None


def test_seek_ignores_a_half_written_snapshot_and_keeps_the_last_complete_one(tmp_path: Path) -> None:
    """A snapshot caught mid-append does not move the seek point off the previous one.

    A snapshot big enough to exceed the atomic-append size is routinely observed
    half-written, so this is the normal case, not corruption. Folding must start
    at the last COMPLETE snapshot; replaying forward from there reaches the torn
    line once the writer finishes it.
    """
    complete = build_agent_details("complete-snapshot-agent")
    append_observe_event(tmp_path, make_full_agent_state_event([complete]))
    events_path = get_observe_events_path(tmp_path)
    offset_of_complete = find_last_full_state_offset(events_path)

    torn = _serialized_event_line(make_full_agent_state_event([build_agent_details("torn-agent")]))
    with open(events_path, "a") as handle:
        handle.write(torn[: len(torn) // 2])

    assert find_last_full_state_offset(events_path) == offset_of_complete


def test_follower_forwards_events_appended_after_it_caught_up(tmp_path: Path) -> None:
    agent = build_agent_details("watched")
    append_observe_event(tmp_path, make_full_agent_state_event([agent]))

    lines: list[str] = []
    follower = ObserveEventFollower(events_base_dir=tmp_path, on_line=lines.append)
    with _running_observer(tmp_path):
        follower.poll_once()
        assert _event_types(lines) == ["AGENTS_FULL_STATE"]
        append_observe_event(tmp_path, make_agent_removed_event(agent.id, agent.name))
        follower.poll_once()

    assert _event_types(lines) == ["AGENTS_FULL_STATE", "AGENT_REMOVED"]


def test_follower_waits_for_a_half_written_line_to_finish(tmp_path: Path) -> None:
    """A partial tail line is left alone until the writer completes it.

    A full-state snapshot of many agents is far larger than the size at which an
    append is atomic, so a reader can legitimately catch one mid-write. Forwarding
    the fragment would raise a JSON error and take the stream down.
    """
    agent = build_agent_details("watched")
    append_observe_event(tmp_path, make_full_agent_state_event([agent]))

    lines: list[str] = []
    follower = ObserveEventFollower(events_base_dir=tmp_path, on_line=lines.append)
    events_path = get_observe_events_path(tmp_path)
    with _running_observer(tmp_path):
        follower.poll_once()

        whole_line = _serialized_event_line(make_agent_state_event(agent))
        split_at = len(whole_line) // 2
        with open(events_path, "a") as handle:
            handle.write(whole_line[:split_at])
        follower.poll_once()
        assert _event_types(lines) == ["AGENTS_FULL_STATE"]

        with open(events_path, "a") as handle:
            handle.write(whole_line[split_at:])
        follower.poll_once()

    assert _event_types(lines) == ["AGENTS_FULL_STATE", "AGENT_STATE"]


def test_follower_forwards_nothing_until_a_snapshot_exists(tmp_path: Path) -> None:
    """With no snapshot ever written, per-agent updates are dropped until one is."""
    agent = build_agent_details("only")
    append_observe_event(tmp_path, make_agent_state_event(agent))

    lines: list[str] = []
    follower = ObserveEventFollower(events_base_dir=tmp_path, on_line=lines.append)
    with _running_observer(tmp_path):
        follower.poll_once()
        assert lines == []

        append_observe_event(tmp_path, make_agent_state_event(agent))
        follower.poll_once()
        assert lines == []

        append_observe_event(tmp_path, make_full_agent_state_event([agent]))
        append_observe_event(tmp_path, make_agent_state_event(agent))
        follower.poll_once()

    assert _event_types(lines) == ["AGENTS_FULL_STATE", "AGENT_STATE"]


def test_follower_reports_the_stream_dead_once_the_observer_exits(tmp_path: Path) -> None:
    """Losing the writer must read as "dead", never as "no new events"."""
    agent = build_agent_details("watched")
    append_observe_event(tmp_path, make_full_agent_state_event([agent]))

    lines: list[str] = []
    follower = ObserveEventFollower(events_base_dir=tmp_path, on_line=lines.append)
    with _running_observer(tmp_path):
        follower.poll_once()
        assert follower.is_alive() is True

    follower.poll_once()

    assert follower.is_alive() is False
    detail = follower.failure_detail()
    assert detail is not None
    assert "exited" in detail


def test_follower_reseeds_after_the_event_file_is_truncated(tmp_path: Path) -> None:
    """A shrunk file means it was replaced, so the stale offset must be abandoned."""
    stale = build_agent_details("stale-agent")
    append_observe_event(tmp_path, make_full_agent_state_event([stale]))

    lines: list[str] = []
    follower = ObserveEventFollower(events_base_dir=tmp_path, on_line=lines.append)
    with _running_observer(tmp_path):
        follower.poll_once()
        get_observe_events_path(tmp_path).write_text("")
        follower.poll_once()

        fresh = build_agent_details("post-truncation-agent")
        append_observe_event(tmp_path, make_full_agent_state_event([fresh]))
        follower.poll_once()

    assert _event_types(lines) == ["AGENTS_FULL_STATE", "AGENTS_FULL_STATE"]
    assert _snapshot_agent_names(lines[-1]) == ["post-truncation-agent"]


def test_started_follower_picks_up_new_events_on_its_own_thread(tmp_path: Path) -> None:
    """End to end through ``start``: the background loop really does deliver."""
    agent = build_agent_details("watched")
    append_observe_event(tmp_path, make_full_agent_state_event([agent]))

    lines: list[str] = []
    follower = ObserveEventFollower(
        events_base_dir=tmp_path,
        on_line=lines.append,
        poll_interval_seconds=0.05,
    )
    with _running_observer(tmp_path):
        follower.start()
        try:
            assert poll_until(lambda: len(lines) == 1, timeout=5.0)
            append_observe_event(tmp_path, make_agent_removed_event(agent.id, agent.name))
            assert poll_until(lambda: len(lines) == 2, timeout=5.0)
        finally:
            follower.stop()

    assert _event_types(lines) == ["AGENTS_FULL_STATE", "AGENT_REMOVED"]
