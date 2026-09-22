from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.chat.chat_seed import SeedRole
from imbue.chat.chat_seed import SeedTurn
from imbue.chat.chat_seed import seed_agent_info
from imbue.chat.chat_seed import seed_event_id
from imbue.chat.chat_seed import seed_events
from imbue.chat.chat_seed import write_seed_file
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.registry import build_loader
from imbue.chat.harnesses.registry import build_watcher
from imbue.chat.harnesses.seed.loader import SeedSessionWatcher
from imbue.chat.primitives import ChatId

_CHAT_ID = ChatId("agent-seeded")


def _seeded_loader(tmp_path: Path, turn_count: int) -> SeedSessionWatcher:
    turns = tuple(
        SeedTurn(role=SeedRole.USER if index % 2 == 0 else SeedRole.ASSISTANT, text=f"turn {index}")
        for index in range(turn_count)
    )
    write_seed_file(tmp_path, seed_events(_CHAT_ID, turns, datetime(2026, 9, 16, tzinfo=timezone.utc)))
    loader = build_loader(seed_agent_info(_CHAT_ID, tmp_path))
    assert isinstance(loader, SeedSessionWatcher)
    return loader


def test_the_seed_loader_answers_every_read_from_the_file_it_read_at_build(tmp_path: Path) -> None:
    loader = _seeded_loader(tmp_path, 5)
    ids = [seed_event_id(_CHAT_ID, index) for index in range(5)]

    assert loader.get_total_event_count() == 5
    assert [event["event_id"] for event in loader.get_all_events()] == ids
    assert [event["event_id"] for event in loader.get_tail_events(2)] == ids[3:]
    assert [event["event_id"] for event in loader.get_backfill_events(ids[3], 2)] == ids[1:3]
    assert [event["event_id"] for event in loader.get_forward_events(ids[1], 2)] == ids[2:4]
    assert [event["event_id"] for event in loader.get_events_at_offset(4, 10)] == ids[4:]
    assert loader.get_event_offset(ids[2]) == 2
    assert loader.get_event_offset("agent-seeded:seed:99") == -1
    assert loader.get_backfill_events("agent-seeded:seed:99", 2) == []
    assert loader.get_tail_events(0) == []


def test_the_seed_harness_watches_nothing_and_its_loader_reads_an_empty_folder_as_empty(tmp_path: Path) -> None:
    """Registered as a harness so the registry can build it, though no agent ever runs on it."""
    info = seed_agent_info(_CHAT_ID, tmp_path / "missing")
    watcher = build_watcher(info, lambda _agent_id, _events: None)
    assert isinstance(watcher, SeedSessionWatcher)
    assert info.harness is HarnessType.SEED
    watcher.start()
    watcher.stop()
    assert watcher.get_total_event_count() == 0
    assert watcher.get_subagent_metadata("anything") is None
