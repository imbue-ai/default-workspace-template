"""Tests for the stops the memory guard made: its log, the shed ledger, the pairing of the two, and the plain descriptions."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from system_monitor import stops

_NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


_SHED_LOG = """\
mem avail:   120 of  3922 MiB ( 3.06%), swap free:    0 of    0 MiB ( 0.00%)
sending SIGTERM to process 60 uid 1000 "node": oom_score 950, VmRSS 400 MiB, cmdline "node old"
process 60 exited after 0.1 seconds
mem avail:    90 of  3922 MiB ( 2.29%), swap free:    0 of    0 MiB ( 0.00%)
sending SIGTERM to process 50 uid 1000 "node": oom_score 1000, VmRSS 700 MiB, cmdline "node /opt/playwright/driver/cli.js
--flag"
process 50 exited after 0.2 seconds
"""


def test_the_shed_log_yields_each_stop_with_its_name_and_full_command(
    tmp_path: Path,
) -> None:
    log = tmp_path / "earlyoom-stderr.log"
    log.write_text(
        _SHED_LOG
        # A line in between that is not part of the command: the stop is left out, not
        # shown with that line glued onto its command.
        + 'sending SIGTERM to process 55 uid 1000 "py": oom_score 9, cmdline "python3 a\n'
        + "escalating to SIGKILL after 0.5 seconds\n"
    )

    entries = stops.read_shed_log(log)

    assert entries == [
        {"pid": 60, "comm": "node", "command": "node old"},
        {
            "pid": 50,
            "comm": "node",
            "command": "node /opt/playwright/driver/cli.js\n--flag",
        },
    ]
    assert stops.read_shed_log(tmp_path / "missing.log") == []


def _ledger_line(pid: int, age: timedelta, **fields: object) -> str:
    timestamp = (_NOW - age).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return json.dumps(
        {"timestamp": timestamp, "type": "process_shed", "pid": pid, **fields}
    )


def test_recent_stops_pair_ledger_records_with_log_entries_and_keep_only_the_last_day(
    tmp_path: Path,
) -> None:
    log = tmp_path / "earlyoom-stderr.log"
    log.write_text(_SHED_LOG)
    ledger = tmp_path / "shed.jsonl"
    ledger.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-10-13T11:00:00.000000Z",
                        "type": "notice_delivered",
                        "agent_name": "x",
                    }
                ),
                _ledger_line(60, timedelta(hours=30), comm="node"),
                _ledger_line(50, timedelta(hours=1), comm="node"),
                "not json",
                _ledger_line(
                    70, timedelta(hours=2), comm="claude", agent_name="Budget-Agent"
                ),
                _ledger_line(80, timedelta(hours=3), comm="python3", is_worker=True),
            ]
        )
    )

    recent = stops.recent_stops(ledger, now=_NOW, log=log)

    assert recent == [
        {
            "at": (_NOW - timedelta(hours=1)).isoformat(),
            "what": "a test browser a chat was using to check an app",
            "command": "node /opt/playwright/driver/cli.js\n--flag",
        },
        {
            "at": (_NOW - timedelta(hours=2)).isoformat(),
            "what": "the Budget chat's agent",
            "command": None,
        },
        {
            "at": (_NOW - timedelta(hours=3)).isoformat(),
            "what": "a background agent",
            "command": None,
        },
    ]
    assert stops.recent_stops(tmp_path / "missing.jsonl", now=_NOW, log=log) == []


def test_a_stop_whose_log_line_was_rotated_away_never_takes_a_later_stops_line(
    tmp_path: Path,
) -> None:
    # The log was rotated: the older stop of pid 4123 lost its line, and a later stop
    # reused pid 4123 under another name. Neither may be shown with the wrong command.
    log = tmp_path / "earlyoom-stderr.log"
    log.write_text(
        'sending SIGTERM to process 4123 uid 1000 "node": oom_score 9, cmdline "node /opt/playwright/driver/x"\n'
    )
    ledger = tmp_path / "shed.jsonl"
    ledger.write_text(
        "\n".join(
            [
                _ledger_line(4123, timedelta(hours=5), comm="claude"),
                _ledger_line(4123, timedelta(hours=1), comm="node"),
            ]
        )
    )

    recent = stops.recent_stops(ledger, now=_NOW, log=log)

    assert [(stop["what"], stop["command"]) for stop in recent] == [
        (
            "a test browser a chat was using to check an app",
            "node /opt/playwright/driver/x",
        ),
        ("a chat's agent", None),
    ]


def test_stopped_commands_are_described_in_the_users_words() -> None:
    names = {"agent-abc": "Budget"}

    assert (
        stops.describe_command("claude -p summarize", names)
        == "a one-off AI task an app or skill started"
    )
    assert (
        stops.describe_command("claude /home/user/.mngr/agents/agent-abc/x", names)
        == "the Budget chat's agent"
    )
    assert (
        stops.describe_command("claude /home/user/.mngr/agents/agent-zzz/x", names)
        == "a chat's agent"
    )
    assert (
        stops.describe_command("node pyright-langserver --stdio", names)
        == "a code checker"
    )
    assert (
        stops.describe_command("node typescript-language-server --stdio", names)
        == "a code checker"
    )
    assert (
        stops.describe_command(
            "/opt/fortress/tilion-fortress/tilion --type=renderer", names
        )
        == "a browser"
    )
    assert (
        stops.describe_command("/usr/bin/rg needle", names)
        == "a background program (rg)"
    )
    assert stops.describe_command("", names) == "a background program"


def test_the_shed_log_is_parsed_again_once_it_changes(tmp_path: Path) -> None:
    log = tmp_path / "earlyoom-stderr.log"
    log.write_text(_SHED_LOG)
    assert [entry["pid"] for entry in stops.read_shed_log(log)] == [60, 50]

    with log.open("a") as appended:
        appended.write(
            'sending SIGTERM to process 90 uid 1000 "x": oom_score 9, cmdline "x"\n'
        )

    assert [entry["pid"] for entry in stops.read_shed_log(log)] == [60, 50, 90]


def test_only_the_end_of_a_long_ledger_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stops, "LEDGER_TAIL_BYTES", 400)
    ledger = tmp_path / "shed.jsonl"
    lines = [
        _ledger_line(pid, timedelta(minutes=100 - pid), comm="node")
        for pid in range(1, 51)
    ]
    ledger.write_text("\n".join(lines) + "\n")

    records = stops.read_ledger(ledger)

    # Only whole records from the last 400 bytes, the newest ones, in order.
    assert 0 < len(records) < 50
    assert [record["pid"] for record in records] == list(range(51 - len(records), 51))
