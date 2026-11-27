"""The workspace's agents as the observer's events file folds to them, and when the file cannot vouch for them."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from workspace_update_control import agents
from workspace_update_control.agents import _TAIL_BLOCK_BYTES
from workspace_update_control.testing import (
    SERVICES_AGENT_ID,
    agent_removed_line,
    agent_state_line,
    full_state_line,
    observed_agent,
    observed_services_agent,
    write_event_lines,
)

_NOW = 1_792_000_000.0
_CHAT_ID = f"agent-{uuid4().hex}"
_OTHER_CHAT_ID = f"agent-{uuid4().hex}"

_LISTED_SERVICES_AGENT = {
    "id": SERVICES_AGENT_ID,
    "name": "system-services",
    "state": "RUNNING",
    "labels.is_primary": "true",
    "labels.chat_id": "",
    "labels.update_dispatch_attempt": "",
}


def _listed_chat(agent_id: str, name: str, state: str) -> dict[str, str]:
    """A chat with no labels of the listing's, as the fold lists it."""
    return {
        "id": agent_id,
        "name": name,
        "state": state,
        "labels.is_primary": "",
        "labels.chat_id": "",
        "labels.update_dispatch_attempt": "",
    }


def test_the_events_file_is_under_the_host_dir_or_else_under_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    under_host_dir = agents.agent_events_path({"MNGR_HOST_DIR": "/srv/mngr-host"})
    under_home = agents.agent_events_path({})

    assert under_host_dir == "/srv/mngr-host/events/mngr/agents/events.jsonl"
    assert under_home == str(
        tmp_path / "home" / ".mngr" / "events" / "mngr" / "agents" / "events.jsonl"
    )


def test_a_snapshot_lists_every_agent_with_the_labels_the_dispatchers_listing_carries(
    events_path: Path,
) -> None:
    attempt_id = uuid4().hex
    write_event_lines(
        events_path,
        [
            full_state_line(
                [
                    observed_services_agent(),
                    observed_agent(
                        _CHAT_ID,
                        " Update-to-v090 ",
                        "WAITING",
                        chat_id=_OTHER_CHAT_ID,
                        update_dispatch_attempt=attempt_id,
                        user_created="true",
                    ),
                ],
                _NOW - 30,
            )
        ],
    )

    listed = agents.read_observed_agents(str(events_path), _NOW)

    assert listed == [
        _LISTED_SERVICES_AGENT,
        {
            "id": _CHAT_ID,
            "name": "Update-to-v090",
            "state": "WAITING",
            "labels.is_primary": "",
            "labels.chat_id": _OTHER_CHAT_ID,
            "labels.update_dispatch_attempt": attempt_id,
        },
    ]


def test_a_label_written_as_a_json_boolean_reads_as_the_listing_prints_it(
    events_path: Path,
) -> None:
    write_event_lines(
        events_path,
        [
            full_state_line(
                [
                    observed_agent(
                        SERVICES_AGENT_ID, "system-services", "RUNNING", is_primary=True
                    ),
                    observed_agent(_CHAT_ID, "my-chat", "RUNNING", is_primary=False),
                ],
                _NOW,
            )
        ],
    )

    listed = agents.read_observed_agents(str(events_path), _NOW)

    assert listed is not None
    assert [agent["labels.is_primary"] for agent in listed] == ["true", "false"]


def test_later_state_and_removal_events_are_applied_over_the_snapshot(
    events_path: Path,
) -> None:
    new_chat_id = f"agent-{uuid4().hex}"
    write_event_lines(
        events_path,
        [
            full_state_line(
                [
                    observed_services_agent(),
                    observed_agent(_CHAT_ID, "my-chat", "RUNNING"),
                    observed_agent(_OTHER_CHAT_ID, "old-chat", "WAITING"),
                ],
                _NOW - 240,
            ),
            agent_state_line(
                observed_agent(_CHAT_ID, "my-chat", "WAITING"), _NOW - 180
            ),
            agent_state_line(
                observed_agent(new_chat_id, "new-chat", "RUNNING"), _NOW - 120
            ),
            agent_removed_line(_OTHER_CHAT_ID, _NOW - 60),
            # A chat renamed since the snapshot is the same agent under its new name.
            agent_state_line(
                observed_agent(_CHAT_ID, "my-renamed-chat", "STOPPED"), _NOW - 30
            ),
        ],
    )

    listed = agents.read_observed_agents(str(events_path), _NOW)

    assert listed == [
        _LISTED_SERVICES_AGENT,
        _listed_chat(_CHAT_ID, "my-renamed-chat", "STOPPED"),
        _listed_chat(new_chat_id, "new-chat", "RUNNING"),
    ]


def test_only_the_last_snapshot_counts_and_what_came_before_it_is_not_read(
    events_path: Path,
) -> None:
    write_event_lines(
        events_path,
        [
            full_state_line(
                [
                    observed_services_agent(),
                    observed_agent(_OTHER_CHAT_ID, "destroyed-since", "RUNNING"),
                ],
                _NOW - 600,
            ),
            agent_state_line(
                observed_agent(_OTHER_CHAT_ID, "destroyed-since", "WAITING"), _NOW - 500
            ),
            full_state_line(
                [
                    observed_services_agent(),
                    observed_agent(_CHAT_ID, "my-chat", "RUNNING"),
                ],
                _NOW - 300,
            ),
        ],
    )

    listed = agents.read_observed_agents(str(events_path), _NOW)

    assert listed == [
        _LISTED_SERVICES_AGENT,
        _listed_chat(_CHAT_ID, "my-chat", "RUNNING"),
    ]


def test_lines_that_are_not_events_and_agents_without_an_id_or_a_name_are_passed_over(
    events_path: Path,
) -> None:
    write_event_lines(
        events_path,
        [
            full_state_line(
                [
                    observed_services_agent(),
                    {"name": "no-id", "state": "RUNNING", "labels": {}},
                    {"id": _OTHER_CHAT_ID, "name": "  ", "state": "RUNNING"},
                    "not an agent",
                    observed_agent(_CHAT_ID, "my-chat", "RUNNING"),
                ],
                _NOW - 30,
            ),
            "{cut off mid-wri",
            "",
            json.dumps(["a", "list"]),
            json.dumps({"timestamp": "not a time", "type": "SOMETHING_NEW"}),
            json.dumps({"type": "AGENT_REMOVED", "agent_id": 7}),
        ],
    )

    listed = agents.read_observed_agents(str(events_path), _NOW)

    assert listed == [
        _LISTED_SERVICES_AGENT,
        _listed_chat(_CHAT_ID, "my-chat", "RUNNING"),
    ]


def test_an_agent_whose_state_is_missing_is_listed_with_no_state(
    events_path: Path,
) -> None:
    write_event_lines(
        events_path,
        [
            full_state_line(
                [
                    observed_services_agent(),
                    {"id": _CHAT_ID, "name": "my-chat", "labels": None},
                ],
                _NOW,
            )
        ],
    )

    listed = agents.read_observed_agents(str(events_path), _NOW)

    assert listed == [_LISTED_SERVICES_AGENT, _listed_chat(_CHAT_ID, "my-chat", "")]


def test_a_missing_file_cannot_vouch_for_the_agents(events_path: Path) -> None:
    assert agents.read_observed_agents(str(events_path), _NOW) is None


def test_an_empty_file_cannot_vouch_for_the_agents(events_path: Path) -> None:
    write_event_lines(events_path, [])

    assert agents.read_observed_agents(str(events_path), _NOW) is None


def test_a_file_with_no_snapshot_cannot_vouch_for_the_agents(events_path: Path) -> None:
    """A state event alone names one agent, and says nothing about which others there are."""
    write_event_lines(
        events_path,
        [
            agent_state_line(observed_services_agent(), _NOW - 60),
            agent_state_line(observed_agent(_CHAT_ID, "my-chat", "RUNNING"), _NOW - 30),
        ],
    )

    assert agents.read_observed_agents(str(events_path), _NOW) is None


def test_a_line_that_only_holds_a_snapshots_type_as_a_name_is_not_a_snapshot(
    events_path: Path,
) -> None:
    named_like_the_type = observed_agent(
        SERVICES_AGENT_ID, "AGENTS_FULL_STATE", "RUNNING", is_primary="true"
    )
    write_event_lines(events_path, [agent_state_line(named_like_the_type, _NOW - 30)])

    assert agents.read_observed_agents(str(events_path), _NOW) is None


@pytest.mark.parametrize(
    ("newest_event_age_seconds", "is_vouched_for"),
    [(10 * 60, True), (10 * 60 + 1, False)],
    ids=("ten-minutes", "past-ten-minutes"),
)
def test_a_file_nothing_has_written_for_over_ten_minutes_cannot_vouch_for_the_agents(
    events_path: Path, newest_event_age_seconds: int, is_vouched_for: bool
) -> None:
    write_event_lines(
        events_path,
        [
            full_state_line([observed_services_agent()], _NOW - 3600),
            agent_state_line(
                observed_agent(_CHAT_ID, "my-chat", "RUNNING"),
                _NOW - newest_event_age_seconds,
            ),
        ],
    )

    listed = agents.read_observed_agents(str(events_path), _NOW)

    assert (listed is not None) is is_vouched_for


def test_events_with_no_readable_time_cannot_vouch_for_the_agents(
    events_path: Path,
) -> None:
    snapshot_event = json.loads(full_state_line([observed_services_agent()], _NOW))
    snapshot_event["timestamp"] = "half past nine"
    untimed_event = dict(snapshot_event)
    del untimed_event["timestamp"]
    write_event_lines(
        events_path, [json.dumps(snapshot_event), json.dumps(untimed_event)]
    )

    assert agents.read_observed_agents(str(events_path), _NOW) is None


def test_a_file_without_the_workspaces_services_agent_cannot_vouch_for_the_agents(
    events_path: Path,
) -> None:
    """Such a file is another host's, and would make every one of this workspace's agents read as gone."""
    write_event_lines(
        events_path,
        [full_state_line([observed_agent(_CHAT_ID, "my-chat", "RUNNING")], _NOW - 30)],
    )

    assert agents.read_observed_agents(str(events_path), _NOW) is None


def test_a_services_agent_removed_since_the_snapshot_leaves_the_file_unable_to_vouch(
    events_path: Path,
) -> None:
    write_event_lines(
        events_path,
        [
            full_state_line(
                [
                    observed_services_agent(),
                    observed_agent(_CHAT_ID, "my-chat", "RUNNING"),
                ],
                _NOW - 60,
            ),
            agent_removed_line(SERVICES_AGENT_ID, _NOW - 30),
        ],
    )

    assert agents.read_observed_agents(str(events_path), _NOW) is None


@pytest.mark.parametrize(
    ("timestamp", "is_vouched_for"),
    [
        # Nine fractional digits, as the observer writes them: a second inside the ten minutes, and one past.
        ("2026-10-14T17:36:40.123456789Z", True),
        ("2026-10-14T17:36:39.999999999Z", False),
        # No fraction, a short one, and an offset in place of the Z.
        ("2026-10-14T17:36:41Z", True),
        ("2026-10-14T17:36:40.5Z", True),
        ("2026-10-14T23:06:40.123456789+05:30", True),
        ("2026-10-14T23:06:39.999999999+05:30", False),
        # A time with no zone is UTC.
        ("2026-10-14T17:36:40.123456", True),
        ("2026-10-14T17:36:39.999999", False),
    ],
)
def test_an_events_time_is_read_whatever_its_fraction_and_its_zone(
    events_path: Path, timestamp: str, is_vouched_for: bool
) -> None:
    """The fold's ``now`` is 2026-10-14T17:46:40Z, so an event from 17:36:40 on is within the ten minutes."""
    snapshot_event = json.loads(full_state_line([observed_services_agent()], _NOW))
    snapshot_event["timestamp"] = timestamp
    write_event_lines(events_path, [json.dumps(snapshot_event)])

    listed = agents.read_observed_agents(str(events_path), _NOW)

    assert (listed == [_LISTED_SERVICES_AGENT]) is is_vouched_for
    assert (listed is None) is not is_vouched_for


def _many_chats(count: int) -> list[dict[str, object]]:
    return [
        observed_agent(f"agent-{uuid4().hex}", f"chat-{number}", "WAITING")
        for number in range(count)
    ]


def test_a_snapshot_line_longer_than_the_blocks_the_file_is_read_in_is_read_whole(
    events_path: Path,
) -> None:
    chats = _many_chats(6000)
    snapshot_line = full_state_line([observed_services_agent(), *chats], _NOW - 120)
    assert len(snapshot_line) > 2 * _TAIL_BLOCK_BYTES
    write_event_lines(
        events_path,
        [
            full_state_line(
                [
                    observed_services_agent(),
                    observed_agent(_OTHER_CHAT_ID, "destroyed-since", "RUNNING"),
                ],
                _NOW - 420,
            ),
            snapshot_line,
            agent_state_line(observed_agent(_CHAT_ID, "my-chat", "RUNNING"), _NOW - 60),
        ],
    )

    listed = agents.read_observed_agents(str(events_path), _NOW)

    assert listed is not None
    assert [agent["name"] for agent in listed] == [
        "system-services",
        *(f"chat-{number}" for number in range(6000)),
        "my-chat",
    ]
    assert [agent["id"] for agent in listed[1:-1]] == [chat["id"] for chat in chats]


@pytest.mark.parametrize("bytes_of_the_marker_in_the_older_block", range(0, 20, 3))
def test_a_snapshot_whose_type_straddles_two_read_blocks_is_still_found(
    events_path: Path, bytes_of_the_marker_in_the_older_block: int
) -> None:
    older_line = full_state_line(
        [
            observed_services_agent(),
            observed_agent(_OTHER_CHAT_ID, "destroyed-since", "RUNNING"),
        ],
        _NOW - 420,
    )
    snapshot_line = full_state_line(
        [observed_services_agent(), observed_agent(_CHAT_ID, "my-chat", "RUNNING")],
        _NOW - 120,
    )
    marker_at = snapshot_line.index('"AGENTS_FULL_STATE"')
    bytes_after_the_split = (
        len(snapshot_line) - marker_at - bytes_of_the_marker_in_the_older_block + 1
    )
    padding = "x" * 64
    later_line = agent_state_line(
        observed_agent(f"agent-{uuid4().hex}", "later-chat", "WAITING", note=padding),
        _NOW - 60,
    )
    # The last block read holds the snapshot's line from the split on, then one later line of the right length.
    padding += "x" * (_TAIL_BLOCK_BYTES - bytes_after_the_split - len(later_line) - 1)
    later_line = later_line.replace("x" * 64, padding)
    write_event_lines(events_path, [older_line, snapshot_line, later_line])
    split_at = os.path.getsize(events_path) - _TAIL_BLOCK_BYTES
    assert (
        split_at
        == len(older_line) + 1 + marker_at + bytes_of_the_marker_in_the_older_block
    )

    listed = agents.read_observed_agents(str(events_path), _NOW)

    assert listed is not None
    assert [agent["name"] for agent in listed] == [
        "system-services",
        "my-chat",
        "later-chat",
    ]
