import json
from pathlib import Path

from share_gateway.assignment import DEFAULT_POLL_SECONDS
from share_gateway.assignment import RelayAssignment
from share_gateway.assignment import parse_assignment
from share_gateway.assignment import read_cached_assignment
from share_gateway.assignment import write_cached_assignment

_RELAY_A = "relay-" + "a" * 16
_RELAY_B = "relay-" + "b" * 16


def _body(entries: list[dict], poll_seconds: object = 60) -> dict:
    return {"workspace_domain": "host-x.user.us1.example", "relay_endpoints": entries, "poll_seconds": poll_seconds}


def test_parse_assignment_reads_every_relay_endpoint() -> None:
    parsed = parse_assignment(
        _body(
            [
                {"relay_id": _RELAY_A, "endpoint": "203.0.113.1:7000"},
                {"relay_id": _RELAY_B, "endpoint": "relay-us1b.example:7001"},
            ]
        )
    )
    assert parsed is not None
    assert parsed.endpoint_by_relay_id == {
        _RELAY_A: ("203.0.113.1", 7000),
        _RELAY_B: ("relay-us1b.example", 7001),
    }
    assert parsed.poll_seconds == 60


def test_parse_assignment_skips_malformed_entries_but_keeps_good_ones() -> None:
    parsed = parse_assignment(
        _body(
            [
                {"relay_id": _RELAY_A, "endpoint": "no-port"},
                {"relay_id": "", "endpoint": "203.0.113.1:7000"},
                "not-a-dict",
                {"relay_id": _RELAY_B, "endpoint": "203.0.113.2:7000"},
            ]
        )
    )
    assert parsed is not None
    assert parsed.endpoint_by_relay_id == {_RELAY_B: ("203.0.113.2", 7000)}


def test_parse_assignment_rejects_bodies_with_no_usable_endpoint() -> None:
    assert parse_assignment(None) is None
    assert parse_assignment({}) is None
    assert parse_assignment(_body([])) is None
    assert parse_assignment(_body([{"relay_id": _RELAY_A, "endpoint": "nope"}])) is None


def test_parse_assignment_defaults_a_bad_poll_interval() -> None:
    parsed = parse_assignment(_body([{"relay_id": _RELAY_A, "endpoint": "203.0.113.1:7000"}], poll_seconds="soon"))
    assert parsed is not None
    assert parsed.poll_seconds == DEFAULT_POLL_SECONDS


def test_cached_assignment_round_trips(tmp_path: Path) -> None:
    cache_path = tmp_path / "assignment.json"
    assignment = RelayAssignment(
        endpoint_by_relay_id={_RELAY_A: ("203.0.113.1", 7000), _RELAY_B: ("203.0.113.2", 7000)},
        poll_seconds=45,
    )

    write_cached_assignment(cache_path, assignment)
    restored = read_cached_assignment(cache_path)

    assert restored == assignment


def test_read_cached_assignment_handles_missing_and_corrupt_files(tmp_path: Path) -> None:
    assert read_cached_assignment(tmp_path / "absent.json") is None
    corrupt_path = tmp_path / "assignment.json"
    corrupt_path.write_text("{not json")
    assert read_cached_assignment(corrupt_path) is None
    empty_path = tmp_path / "empty.json"
    empty_path.write_text(json.dumps({"relay_endpoints": []}))
    assert read_cached_assignment(empty_path) is None
