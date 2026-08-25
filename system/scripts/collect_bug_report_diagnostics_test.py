"""Tests for the resident bug-report diagnostics collector.

Each test loads its own fresh copy of the script as a module and rebinds the
container paths it hardcodes (the workspace layout, the supervisord log dir,
mngr's agent tree, the secret-scan gate) at tmp fixtures, then drives the
helpers or ``main`` directly. The scan gate is exercised end to end through
stub ``scan_secrets.sh`` scripts on disk that reproduce the real gate's output
markers verbatim.
"""

import base64
import importlib.util
import io
import json
import os
import shlex
import time
from datetime import datetime, timezone
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT_PATH = Path(__file__).parent / "collect_bug_report_diagnostics.py"

# A supervisord.conf in the shape the workspace writes: every service wrapped in
# oom_tag_service.py, user-created apps passing the literal band "user", and one
# built-in (cron) with no wrapper at all.
_FIXTURE_SUPERVISORD_CONF = """\
[supervisord]
logfile=/var/log/supervisor/supervisord.log

[program:system_interface]
command=python3 system/services/oom_priority/bin/oom_tag_service.py system_interface uv run system-interface

[program:terminal]
command=python3 system/services/oom_priority/bin/oom_tag_service.py terminal uv run terminal

[program:xvfb]
command=python3 system/services/oom_priority/bin/oom_tag_service.py xvfb Xvfb :99 -screen 0 1920x1080x24

[program:cron]
command=/usr/sbin/cron -f

[program:geopolitical-dashboard]
command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "uv run geopolitical-dashboard"

[eventlistener:oom-tag-backstop]
command=python3 system/services/oom_priority/bin/oom_tag_backstop.py
"""


def _rebind(namespace: dict[str, Any], name: str, value: object) -> None:
    """Rebind one of the script's module-level names, failing loudly if it is gone."""
    assert name in namespace, (
        f"the collector no longer defines {name}; update this test"
    )
    namespace[name] = value


def _load_collector(
    *,
    supervisor_log_dir: Path | None = None,
    mngr_binary: Path | None = None,
    supervisord_conf: Path | None = None,
    workspace_dir: Path | None = None,
    scan_gate_dir: Path | None = None,
) -> ModuleType:
    """Load a fresh copy of the collector with its container paths redirected."""
    spec = importlib.util.spec_from_file_location(
        "collect_bug_report_diagnostics_under_test", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    namespace = module.__dict__
    if supervisor_log_dir is not None:
        _rebind(namespace, "SUPERVISOR_LOG_DIR", str(supervisor_log_dir))
    if mngr_binary is not None:
        _rebind(namespace, "MNGR_BINARY", str(mngr_binary))
    if supervisord_conf is not None:
        _rebind(namespace, "SUPERVISORD_CONF", str(supervisord_conf))
    if workspace_dir is not None:
        _rebind(namespace, "WORKSPACE_DIR", str(workspace_dir))
    if scan_gate_dir is not None:
        _rebind(namespace, "SCAN_GATE_DIR", str(scan_gate_dir))
    return module


def _write_log(log_dir: Path, name: str, *, mtime: float, content: str = "") -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_text(content, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _stamp_seconds_ago(age_seconds: float) -> str:
    """An ISO-8601 ``Z`` timestamp that many seconds before now, as transcripts carry them."""
    return (
        datetime.fromtimestamp(time.time() - age_seconds, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _chat_events(marker: str, *, age_seconds: float = 60.0, source: str = "claude") -> str:
    """One conversation's JSONL, carrying ``marker`` and a timestamp of that age.

    The timestamp is what the collector reads for recency (mngr's
    user_activity_time is unpopulated on real agents), and ``marker`` is what a
    content assertion looks for inside the archived member.
    """
    return (
        json.dumps(
            {
                "timestamp": _stamp_seconds_ago(age_seconds),
                "type": "user_message",
                "source": f"{source}/common_transcript",
                "seq": marker,
            }
        )
        + "\n"
    )


def _mngr_stub_for_chats(tmp_path: Path, chats: Mapping[str, str]) -> Path:
    """An mngr stub answering for exactly these ``agent name -> events`` conversations."""
    return _write_mngr_stub(tmp_path, agents=tuple(chats), events_by_agent=chats)


def _write_mngr_stub(
    tmp_path: Path,
    *,
    agents: Sequence[str] = (),
    events_by_agent: Mapping[str, str] | None = None,
    exit_code: int = 0,
) -> Path:
    """Write a stub standing in for the workspace's mngr.

    The collector asks mngr two things -- which agents exist, and what was said
    in one -- so the stub answers exactly those two shapes: the pipe template
    ``{name}|{type}`` for ``list``, and raw JSONL for ``event``.
    """
    events_dir = tmp_path / "stub-events"
    events_dir.mkdir(parents=True, exist_ok=True)
    for agent_name, events in (events_by_agent or {}).items():
        (events_dir / agent_name).write_text(events, encoding="utf-8")
    listing = "".join(f"{name}|chat\n" for name in agents)
    listing_path = tmp_path / "stub-listing.txt"
    listing_path.write_text(listing, encoding="utf-8")
    argv_log = tmp_path / "stub-argv.log"
    script = tmp_path / "mngr-stub"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{argv_log}"\n'
        f"if [ {exit_code} -ne 0 ]; then exit {exit_code}; fi\n"
        'if [ "$1" = "list" ]; then\n'
        f'  cat "{listing_path}"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "event" ]; then\n'
        f'  f="{events_dir}/$2"\n'
        '  if [ -f "$f" ]; then cat "$f"; fi\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _transcript_events(*messages: str, source: str = "claude", timestamp: str = "2026-08-17T12:00:00Z") -> str:
    """JSONL in the shape ``mngr event`` returns, one event per message."""
    return "".join(
        json.dumps(
            {
                "timestamp": timestamp,
                "type": "user_message",
                "source": f"{source}/common_transcript",
                "message": message,
            }
        )
        + "\n"
        for message in messages
    )


def _user_message_line(timestamp: str) -> str:
    """One common-transcript user message, as the fallback ranking reads it."""
    return (
        json.dumps({"type": "user_message", "timestamp": timestamp, "message": "hi"})
        + "\n"
    )


def _write_stub_scan_gate(
    scan_gate_dir: Path, *, exit_code: int, stderr: str = "", stdout: str = ""
) -> None:
    """Stand in for the template's scan gate, reproducing one of its outcomes verbatim.

    The real scan_secrets.sh is driven as a subprocess, so a stub on disk
    exercises ``scan_targets`` end to end rather than around it.
    """
    scan_gate_dir.mkdir(parents=True, exist_ok=True)
    (scan_gate_dir / "betterleaks.toml").write_text("", encoding="utf-8")
    lines = ["#!/bin/sh"]
    for line in stdout.splitlines():
        lines.append(f"echo {json.dumps(line)}")
    for line in stderr.splitlines():
        lines.append(f"echo {json.dumps(line)} >&2")
    lines.append(f"exit {exit_code}")
    (scan_gate_dir / "scan_secrets.sh").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _write_content_matching_stub_scan_gate(scan_gate_dir: Path, *, secret: str) -> None:
    """A stub gate that flags whichever staged targets actually contain ``secret``.

    ``_write_stub_scan_gate``'s canned output has to name a staged path the test
    predicts. This one reads its targets instead, so what it reports is evidence
    about the bytes the collector staged rather than about a filename -- which is
    the whole question for an attachment assembled from several files.
    """
    scan_gate_dir.mkdir(parents=True, exist_ok=True)
    (scan_gate_dir / "betterleaks.toml").write_text("", encoding="utf-8")
    # The gate is invoked as ``bash <script> --config <config> <target>...``,
    # so the two leading arguments are skipped rather than scanned.
    (scan_gate_dir / "scan_secrets.sh").write_text(
        "#!/bin/sh\n"
        "status=0\n"
        'for target in "$@"; do\n'
        '  case "$target" in --config|*.toml) continue ;; esac\n'
        f'  if grep -qF {shlex.quote(secret)} "$target"; then\n'
        '    echo "scan_secrets.sh: SECRET SCAN FINDING [betterleaks] aws-key: $target:1 (value redacted)" >&2\n'
        "    status=1\n"
        "  fi\n"
        "done\n"
        "exit $status\n",
        encoding="utf-8",
    )


def _write_sleeping_stub_scan_gate(
    scan_gate_dir: Path, *, sleep_seconds: float
) -> None:
    scan_gate_dir.mkdir(parents=True, exist_ok=True)
    (scan_gate_dir / "betterleaks.toml").write_text("", encoding="utf-8")
    (scan_gate_dir / "scan_secrets.sh").write_text(
        f"#!/bin/sh\nsleep {sleep_seconds}\nexit 0\n", encoding="utf-8"
    )


def _zip_from_payload(payload: dict[str, Any]) -> zipfile.ZipFile:
    """The archive a payload carries, decoded back out of its base64."""
    return zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["zip"], validate=True)))


# --- Caps and contract constants ---


def test_the_scan_gate_path_points_at_a_gate_that_exists_in_this_repo() -> None:
    """The gate is addressed by a hardcoded path, so a rename silently disables it.

    Failing closed means a missing gate costs the report every attachment rather
    than leaking anything -- correct, but indistinguishable from a workspace
    that simply has no scanner. This caught exactly that: the skill was renamed
    publish-inspiration -> publish-template in the template, and the collector
    kept pointing at the old path, so every CI collection reported
    scanner_unavailable.
    """
    module = _load_collector()
    repo_root = Path(__file__).resolve().parents[2]
    gate_dir = repo_root / Path(module.SCAN_GATE_DIR).relative_to(module.WORKSPACE_DIR)

    assert (gate_dir / "scan_secrets.sh").is_file(), f"no scan gate at {gate_dir}"
    assert (gate_dir / "betterleaks.toml").is_file(), f"no scanner config at {gate_dir}"


def test_collector_caps_match_the_documented_limits() -> None:
    module = _load_collector()
    assert module.CONTRACT_VERSION == 1
    assert module.MAX_LOG_FILES == 100
    assert module.MAX_LINES_PER_LOG == 200
    assert module.MIN_TRANSCRIPT_COUNT == 5
    assert module.LOG_RECENCY_WINDOW_SECONDS == 24 * 60 * 60


def test_select_log_files_caps_at_the_newest_hundred_files(tmp_path: Path) -> None:
    log_dir = tmp_path / "supervisor"
    over_cap_count = 120
    now = time.time()
    # All inside the day window (the recency filter has its own test); one
    # second apart so newest-first is a strict order.
    for index in range(over_cap_count):
        _write_log(log_dir, f"svc-{index:03d}-stderr.log", mtime=now - index)
    module = _load_collector(supervisor_log_dir=log_dir)

    selected = module.select_log_files()

    assert len(selected) == module.MAX_LOG_FILES
    expected_newest_first = [
        str(log_dir / f"svc-{index:03d}-stderr.log") for index in range(module.MAX_LOG_FILES)
    ]
    assert selected == expected_newest_first


def test_select_log_files_drops_logs_not_written_to_in_the_last_day(
    tmp_path: Path,
) -> None:
    """A service that has been silent for over a day is history, not diagnostics."""
    log_dir = tmp_path / "supervisor"
    now = time.time()
    _write_log(log_dir, "system_interface-stderr.log", mtime=now - 60)
    _write_log(log_dir, "terminal-stderr.log", mtime=now - 23 * 60 * 60)
    _write_log(log_dir, "xvfb-stderr.log", mtime=now - 25 * 60 * 60)
    module = _load_collector(supervisor_log_dir=log_dir)

    selected = module.select_log_files()

    assert [os.path.basename(path) for path in selected] == [
        "system_interface-stderr.log",
        "terminal-stderr.log",
    ]


# --- User-app log exclusion ---


def test_select_log_files_keeps_every_programs_logs_both_streams(
    tmp_path: Path,
) -> None:
    """No log is filtered by owner or stream: an app's log -- user-created or
    built-in, stdout or stderr, supervisord's own included -- can carry the bug,
    so all of them ride (still day-bounded, capped, and secret-scanned)."""
    log_dir = tmp_path / "supervisor"
    now = time.time()
    _write_log(log_dir, "supervisord.log", mtime=now)
    _write_log(log_dir, "system_interface-stderr.log", mtime=now - 1)
    _write_log(log_dir, "system_interface-stdout.log", mtime=now - 2)
    _write_log(log_dir, "terminal-stdout.log", mtime=now - 3)
    _write_log(log_dir, "geopolitical-dashboard-stderr.log", mtime=now - 4)
    module = _load_collector(supervisor_log_dir=log_dir)

    selected = module.select_log_files()

    assert [os.path.basename(path) for path in selected] == [
        "supervisord.log",
        "system_interface-stderr.log",
        "system_interface-stdout.log",
        "terminal-stdout.log",
        "geopolitical-dashboard-stderr.log",
    ]


# --- Chat selection ---


def test_every_agent_is_a_transcript_candidate(
    tmp_path: Path,
) -> None:
    """No agent is filtered out: chat, background worker, or the primary services
    agent -- any of their conversations can carry the bug. An agent with no
    transcript simply contributes no member downstream.
    """
    stub = _write_mngr_stub(
        tmp_path,
        agents=("chatty", "system-services", "worker"),
    )
    collector = _load_collector(mngr_binary=stub)

    assert collector.list_agents(5.0) == ["chatty", "system-services", "worker"]


def test_a_transcript_is_named_for_the_harness_that_wrote_it_not_the_agent_type(
    tmp_path: Path,
) -> None:
    """An agent of type ``chat`` writes its events under ``claude/``.

    The two do not map onto each other, so the harness is read from the events'
    own source rather than derived from the type -- deriving it produced a
    source that does not exist and silently collected nothing.
    """
    stub = _write_mngr_stub(
        tmp_path,
        agents=("chatty",),
        events_by_agent={"chatty": _transcript_events("hello", source="claude")},
    )
    collector = _load_collector(mngr_binary=stub)

    members = collector.collect_transcript_members(5.0)

    assert [name for name, _, _ in members] == ["chats/chatty-claude.jsonl"]


def test_the_transcript_query_asks_for_conversations_and_excludes_the_converter_log(
    tmp_path: Path,
) -> None:
    """What the collector ASKS mngr for is the contract, and it is easy to get wrong.

    The harness cannot be derived from the agent type (a ``chat`` agent writes
    under ``claude/``), so the query filters on the source each event carries.
    Everything under ``logs/`` is the converter's own stdout -- it records *that*
    it converted, not what was said -- so including it would attach a log of
    conversions in place of the conversation. Asserting the arguments is what
    catches a wrong filter: a stub that answered regardless of them let a
    deliberately broken source stay green.
    """
    chats = {"chatty": _chat_events("hello")}
    stub = _mngr_stub_for_chats(tmp_path, chats)
    module = _load_collector(mngr_binary=stub)

    module.collect_transcript_members(5.0)

    invocations = (tmp_path / "stub-argv.log").read_text(encoding="utf-8")
    event_calls = [line for line in invocations.splitlines() if line.startswith("event ")]
    assert len(event_calls) == 1, invocations
    assert 'source.endsWith("common_transcript")' in event_calls[0]
    assert 'source.startsWith("logs/")' in event_calls[0]
    assert "--format jsonl" in event_calls[0]


def test_an_agent_with_no_conversation_contributes_no_member(tmp_path: Path) -> None:
    stub = _write_mngr_stub(tmp_path, agents=("chatty",))
    collector = _load_collector(mngr_binary=stub)

    assert collector.collect_transcript_members(5.0) == []


def test_every_chat_written_to_inside_the_window_rides_along_newest_first(
    tmp_path: Path,
) -> None:
    """A bug is rarely about exactly one conversation: a busy window sends more
    than the floor, and a chat outside the window past the floor stays home."""
    # Six chats inside the two-hour window (one more than the floor) and one
    # outside it: every recent chat rides, the idle one does not.
    recent_names = [f"busy-{index}" for index in range(6)]
    events_by_agent = {
        name: _transcript_events(name, timestamp=_stamp_seconds_ago(60 * (index + 1)))
        for index, name in enumerate(recent_names)
    }
    events_by_agent["idle"] = _transcript_events("idle", timestamp=_stamp_seconds_ago(10_000))
    stub = _write_mngr_stub(
        tmp_path,
        agents=tuple([*recent_names, "idle"]),
        events_by_agent=events_by_agent,
    )
    collector = _load_collector(mngr_binary=stub)

    members = collector.collect_transcript_members(5.0)

    assert [name for name, _, _ in members] == [
        f"chats/busy-{index}-claude.jsonl" for index in range(6)
    ]


def test_a_quiet_window_still_sends_the_five_newest_chats(
    tmp_path: Path,
) -> None:
    """The floor: at least MIN_TRANSCRIPT_COUNT chats ride when the workspace has
    them, however quiet the window -- a bug filed from a quiet workspace still
    needs its recent history."""
    # One chat inside the window, six outside it: the window's one plus the
    # next-newest four make the floor of five; the two oldest stay home.
    events_by_agent = {
        "fresh": _transcript_events("fresh", timestamp=_stamp_seconds_ago(60)),
        **{
            f"stale-{index}": _transcript_events(
                f"stale-{index}", timestamp=_stamp_seconds_ago(10_000 + 100 * index)
            )
            for index in range(6)
        },
    }
    stub = _write_mngr_stub(
        tmp_path,
        agents=tuple(events_by_agent),
        events_by_agent=events_by_agent,
    )
    collector = _load_collector(mngr_binary=stub)

    members = collector.collect_transcript_members(5.0)

    assert [name for name, _, _ in members] == [
        "chats/fresh-claude.jsonl",
        "chats/stale-0-claude.jsonl",
        "chats/stale-1-claude.jsonl",
        "chats/stale-2-claude.jsonl",
        "chats/stale-3-claude.jsonl",
    ]


def test_an_idle_workspace_still_carries_its_most_recent_chats(
    tmp_path: Path,
) -> None:
    """Nothing was touched in the window, and a stale workspace is exactly where
    the conversation is hardest to reconstruct from anything else. Fewer chats
    than the floor means all of them ride."""
    stub = _write_mngr_stub(
        tmp_path,
        agents=("stale", "staler"),
        events_by_agent={
            "stale": _transcript_events("recent-ish", timestamp="2026-08-01T12:00:00Z"),
            "staler": _transcript_events("ancient", timestamp="2025-01-01T12:00:00Z"),
        },
    )
    collector = _load_collector(mngr_binary=stub)

    members = collector.collect_transcript_members(5.0)

    assert [name for name, _, _ in members] == [
        "chats/stale-claude.jsonl",
        "chats/staler-claude.jsonl",
    ]


def test_a_workspace_whose_mngr_cannot_be_asked_reports_no_chats(
    tmp_path: Path,
) -> None:
    """A failing mngr must read as no transcript, never as a crash: the collector
    asks mngr precisely so it does not re-derive this from the files itself."""
    stub = _write_mngr_stub(tmp_path, agents=("chatty",), exit_code=3)
    collector = _load_collector(mngr_binary=stub)

    assert collector.list_agents(5.0) == []
    assert collector.collect_transcript_members(5.0) == []


def test_transcript_member_names_live_under_chats_and_cannot_escape_an_extraction_directory() -> (
    None
):
    """Member names are built from names mngr reports, so they are sanitized
    before they can reach a reader's filesystem on extraction."""
    module = _load_collector()

    name = module.transcript_member_name("../../../etc/passwd", "claude", set())

    assert name.startswith("chats/"), name
    remainder = name[len("chats/") :]
    assert "/" not in remainder, name
    assert ".." not in remainder, name
    assert not remainder.startswith("."), name


def test_transcript_member_names_stay_unique_within_one_archive() -> None:
    """Two chats that resolve to the same agent and harness must not collide: a
    zip member silently overwriting another would drop a whole conversation."""
    module = _load_collector()
    used: set[str] = set()

    first = module.transcript_member_name("agent-a", "claude", used)
    second = module.transcript_member_name("agent-a", "claude", used)

    assert first == "chats/agent-a-claude.jsonl"
    assert second != first
    assert second.endswith(".jsonl")


# --- Zip timestamp clamps ---


def test_zip_records_an_epoch_fallback_mtime_at_the_formats_floor() -> None:
    """A transcript whose mtime falls back to the 1970 epoch is clamped to the
    earliest instant a zip can record, rather than failing the whole archive --
    zip rejects any timestamp before 1980."""
    module = _load_collector()

    archive_bytes = module.build_zip(
        [("chats/agent-a-claude.jsonl", '{"seq": 1}\n', 0.0)]
    )

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        assert (
            archive.read("chats/agent-a-claude.jsonl").decode("utf-8") == '{"seq": 1}\n'
        )
        assert archive.infolist()[0].date_time[0] == 1980


def test_zip_records_a_far_future_chat_at_the_formats_ceiling() -> None:
    """A zip date packs the year into 7 bits from 1980, so 2108 does not fit.

    An unclamped far-future mtime raises mid-archive, after the scan cleared
    everything, which would cost the report every attachment. Clock skew and a
    restored or touched file both reach this.
    """
    module = _load_collector()
    far_future = time.mktime((2110, 5, 1, 12, 0, 0, 0, 0, -1))

    archive_bytes = module.build_zip(
        [("chats/agent-a-claude.jsonl", '{"seq": 1}\n', far_future)]
    )

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        # The last instant the format holds, read back a second early because a
        # DOS time records seconds in two-second steps.
        assert archive.infolist()[0].date_time == (2107, 12, 31, 23, 59, 58)


# --- Scan verdicts (fail closed) ---


def test_scan_targets_releases_every_file_on_a_clean_scan(tmp_path: Path) -> None:
    gate = tmp_path / "gate"
    _write_stub_scan_gate(gate, exit_code=0)
    module = _load_collector(scan_gate_dir=gate)

    verdicts = module.scan_targets(["/tmp/logs.txt", "/tmp/transcript.txt"], 10)

    assert verdicts == {"/tmp/logs.txt": None, "/tmp/transcript.txt": None}


def test_scan_targets_drops_only_the_file_a_finding_names(tmp_path: Path) -> None:
    gate = tmp_path / "gate"
    _write_stub_scan_gate(
        gate,
        exit_code=1,
        stderr="scan_secrets.sh: SECRET SCAN FINDING [betterleaks] aws-key: /tmp/transcript.txt:4 (value redacted)",
    )
    module = _load_collector(scan_gate_dir=gate)

    verdicts = module.scan_targets(["/tmp/logs.txt", "/tmp/transcript.txt"], 10)

    assert verdicts == {"/tmp/logs.txt": None, "/tmp/transcript.txt": "secrets_found"}


@pytest.mark.parametrize(
    "marker",
    [
        "scan_secrets.sh: TARGET MISSING: /tmp/logs.txt does not exist (refusing to scan-as-clean)",
        "scan_secrets.sh: SCANNER MISSING: 'kingfisher' is not installed. Refusing to scan without it.",
        "scan_secrets.sh: CONFIG MISSING: betterleaks config not found at /tmp/betterleaks.toml",
        "scan_secrets.sh: SCANNER ERROR: betterleaks exited 2 (not a clean/findings exit); failing the scan",
    ],
)
def test_scan_targets_disqualifies_every_file_when_a_scanner_did_not_complete(
    tmp_path: Path, marker: str
) -> None:
    """A file only one of the two mandatory scanners looked at has not been scanned.

    The malfunction markers can arrive alongside a genuine finding for some other
    file, so they are decided before any finding line is read.
    """
    gate = tmp_path / "gate"
    _write_stub_scan_gate(
        gate,
        exit_code=1,
        stderr=marker
        + "\nscan_secrets.sh: SECRET SCAN FINDING [kingfisher] aws-key: /tmp/transcript.txt:4 (value redacted)",
    )
    module = _load_collector(scan_gate_dir=gate)

    verdicts = module.scan_targets(["/tmp/logs.txt", "/tmp/transcript.txt"], 10)

    assert verdicts == {
        "/tmp/logs.txt": "scanner_unavailable",
        "/tmp/transcript.txt": "scanner_unavailable",
    }


def test_scan_targets_drops_every_file_when_a_report_could_not_be_parsed(
    tmp_path: Path,
) -> None:
    gate = tmp_path / "gate"
    _write_stub_scan_gate(
        gate,
        exit_code=1,
        stderr="scan_secrets.sh: SECRET SCAN FAILED (kingfisher found leaks but its report could not be parsed)",
    )
    module = _load_collector(scan_gate_dir=gate)

    verdicts = module.scan_targets(["/tmp/logs.txt", "/tmp/transcript.txt"], 10)

    assert verdicts == {
        "/tmp/logs.txt": "secrets_found",
        "/tmp/transcript.txt": "secrets_found",
    }


def test_scan_targets_drops_every_file_when_a_finding_names_none_of_them(
    tmp_path: Path,
) -> None:
    gate = tmp_path / "gate"
    _write_stub_scan_gate(
        gate,
        exit_code=1,
        stderr="scan_secrets.sh: SECRET SCAN FINDING [betterleaks] aws-key: /tmp/elsewhere.txt:9 (value redacted)",
    )
    module = _load_collector(scan_gate_dir=gate)

    verdicts = module.scan_targets(["/tmp/logs.txt", "/tmp/transcript.txt"], 10)

    assert verdicts == {
        "/tmp/logs.txt": "secrets_found",
        "/tmp/transcript.txt": "secrets_found",
    }


def test_scan_targets_drops_every_file_when_a_failed_scan_said_nothing(
    tmp_path: Path,
) -> None:
    gate = tmp_path / "gate"
    _write_stub_scan_gate(gate, exit_code=1)
    module = _load_collector(scan_gate_dir=gate)

    verdicts = module.scan_targets(["/tmp/logs.txt"], 10)

    assert verdicts == {"/tmp/logs.txt": "scanner_unavailable"}


def test_scan_targets_fails_closed_when_the_scan_gate_is_absent(tmp_path: Path) -> None:
    """A workspace missing the template's gate must never release anything."""
    module = _load_collector(scan_gate_dir=tmp_path / "no-such-gate")

    verdicts = module.scan_targets(["/tmp/logs.txt"], 10)

    assert verdicts == {"/tmp/logs.txt": "scanner_unavailable"}


def test_scan_targets_fails_closed_when_the_scan_times_out(tmp_path: Path) -> None:
    gate = tmp_path / "gate"
    _write_sleeping_stub_scan_gate(gate, sleep_seconds=5)
    module = _load_collector(scan_gate_dir=gate)

    verdicts = module.scan_targets(["/tmp/logs.txt", "/tmp/transcript.txt"], 0.5)

    assert verdicts == {
        "/tmp/logs.txt": "scanner_unavailable",
        "/tmp/transcript.txt": "scanner_unavailable",
    }


# --- End-to-end output shape ---


def test_main_prints_only_the_contract_json_line_with_all_content_on_a_clean_scan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The clean path: exactly one JSON line (no sentinels -- those belong to the
    invoking shell), carrying one zip with the logs member and each recent chat
    as its own member, newest chat first."""
    gate = tmp_path / "gate"
    _write_stub_scan_gate(gate, exit_code=0)
    log_dir = tmp_path / "supervisor"
    _write_log(
        log_dir,
        "system_interface-stderr.log",
        mtime=time.time(),
        content="interface started\n",
    )
    conf = tmp_path / "supervisord.conf"
    conf.write_text(_FIXTURE_SUPERVISORD_CONF, encoding="utf-8")
    now = time.time()
    chats = {
        "agent-older": _chat_events("agent-older", age_seconds=600, source="claude"),
        "agent-newer": _chat_events("agent-newer", age_seconds=60, source="codex"),
    }
    mngr_stub = _mngr_stub_for_chats(tmp_path, chats)
    module = _load_collector(
        supervisor_log_dir=log_dir,
        mngr_binary=mngr_stub,
        supervisord_conf=conf,
        workspace_dir=tmp_path / "workspace",
        scan_gate_dir=gate,
    )

    module.main(["--logs", "--transcript"])

    out = capsys.readouterr().out
    assert out.endswith("\n") and out.count("\n") == 1, (
        "the collector must print exactly one line"
    )
    payload = json.loads(out)
    assert payload["contract_version"] == 1
    assert payload["omissions"] == {}
    with _zip_from_payload(payload) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == [
            "metadata.json",
            "logs/system_interface.log",
            "chats/agent-newer-codex.jsonl",
            "chats/agent-older-claude.jsonl",
        ]
        # The service log is its own member; the structured context is json.
        assert (
            "interface started"
            in archive.read("logs/system_interface.log").decode("utf-8")
        )
        metadata = json.loads(archive.read("metadata.json").decode("utf-8"))
        assert set(metadata) == {"workspace", "host_health", "services"}
        assert '"seq": "agent-newer"' in archive.read(
            "chats/agent-newer-codex.jsonl"
        ).decode("utf-8")
        assert '"seq": "agent-older"' in archive.read(
            "chats/agent-older-claude.jsonl"
        ).decode("utf-8")
        # Each chat's own last-modified time rides on its member.
        modified_year_by_name = {
            info.filename: info.date_time[0] for info in archive.infolist()
        }
        assert (
            modified_year_by_name["chats/agent-newer-codex.jsonl"]
            == time.gmtime(now).tm_year
        )


def test_main_reports_no_chat_transcript_when_the_agent_tree_is_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_collector(mngr_binary=_mngr_stub_for_chats(tmp_path, {}))

    module.main(["--transcript"])

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "contract_version": 1,
        "omissions": {"transcript": "no_chat_transcript"},
    }
    assert "zip" not in payload


def test_main_omits_an_unrequested_content_type_from_both_zip_and_omissions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--logs alone must not mention the transcript anywhere, even in a workspace
    that has chats -- an unrequested type simply does not appear."""
    gate = tmp_path / "gate"
    _write_stub_scan_gate(gate, exit_code=0)
    log_dir = tmp_path / "supervisor"
    _write_log(
        log_dir,
        "system_interface-stderr.log",
        mtime=time.time(),
        content="interface started\n",
    )
    conf = tmp_path / "supervisord.conf"
    conf.write_text(_FIXTURE_SUPERVISORD_CONF, encoding="utf-8")
    chats = {"agent-a": _chat_events("chatty")}
    module = _load_collector(
        supervisor_log_dir=log_dir,
        mngr_binary=_mngr_stub_for_chats(tmp_path, chats),
        supervisord_conf=conf,
        workspace_dir=tmp_path / "workspace",
        scan_gate_dir=gate,
    )

    module.main(["--logs"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["omissions"] == {}
    with _zip_from_payload(payload) as archive:
        # Logs only: metadata plus the service logs, and no chats member at all.
        assert archive.namelist() == ["metadata.json", "logs/system_interface.log"]


def test_main_withholds_the_whole_archive_when_one_chat_carries_a_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One chat's finding withholds every conversation, not just that one.

    A partial archive is worse than none: a reader cannot tell it from the full
    set, so the conversations that were dropped look like conversations that
    never happened. The clean chat here is the proof -- it would have shipped if
    the verdict were taken per file.
    """
    secret = "AKIAIOSFODNN7EXAMPLE"
    gate = tmp_path / "gate"
    _write_content_matching_stub_scan_gate(gate, secret=secret)
    chats = {
        "agent-clean": _chat_events("agent-clean"),
        "agent-leaky": _chat_events("agent-leaky") + f'{{"note": "{secret}"}}\n',
    }
    module = _load_collector(
        mngr_binary=_mngr_stub_for_chats(tmp_path, chats), scan_gate_dir=gate
    )

    module.main(["--transcript"])

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "contract_version": 1,
        "omissions": {"transcript": "secrets_found"},
    }
    assert "zip" not in payload


def test_main_scans_the_member_name_a_chat_will_be_archived_under(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The member name goes into the zip's directory in plaintext, so it is
    scanned too. Names are built from agent directory names inside the
    workspace, and the sanitizer that shapes them keeps every character a
    credential is written with. Here the conversation itself is clean and only
    the directory name is not.
    """
    secret = "AKIAIOSFODNN7EXAMPLE"
    gate = tmp_path / "gate"
    _write_content_matching_stub_scan_gate(gate, secret=secret)
    chats = {f"agent-{secret}": _chat_events("clean")}
    module = _load_collector(
        mngr_binary=_mngr_stub_for_chats(tmp_path, chats), scan_gate_dir=gate
    )

    module.main(["--transcript"])

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "contract_version": 1,
        "omissions": {"transcript": "secrets_found"},
    }


def test_main_withholds_only_the_logs_when_the_finding_is_in_the_logs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A finding in the logs costs the logs alone; the clean chats still ship.
    The content-matching gate proves the scan read the staged logs PLAINTEXT --
    the finding is against bytes the collector staged, not a predicted name."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    gate = tmp_path / "gate"
    _write_content_matching_stub_scan_gate(gate, secret=secret)
    log_dir = tmp_path / "supervisor"
    _write_log(
        log_dir,
        "system_interface-stderr.log",
        mtime=time.time(),
        content=f"token leaked: {secret}\n",
    )
    conf = tmp_path / "supervisord.conf"
    conf.write_text(_FIXTURE_SUPERVISORD_CONF, encoding="utf-8")
    chats = {"agent-clean": _chat_events("clean")}
    module = _load_collector(
        supervisor_log_dir=log_dir,
        mngr_binary=_mngr_stub_for_chats(tmp_path, chats),
        supervisord_conf=conf,
        workspace_dir=tmp_path / "workspace",
        scan_gate_dir=gate,
    )

    module.main(["--logs", "--transcript"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["omissions"] == {"workspace_logs": "secrets_found"}
    with _zip_from_payload(payload) as archive:
        # Every logs member goes, metadata included; the clean chat still ships.
        assert archive.namelist() == ["chats/agent-clean-claude.jsonl"]


def test_main_reports_everything_scanner_unavailable_when_the_gate_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A workspace without the scan gate fails closed for every requested type."""
    log_dir = tmp_path / "supervisor"
    _write_log(
        log_dir,
        "system_interface-stderr.log",
        mtime=time.time(),
        content="interface started\n",
    )
    conf = tmp_path / "supervisord.conf"
    conf.write_text(_FIXTURE_SUPERVISORD_CONF, encoding="utf-8")
    chats = {"agent-a": _chat_events("chatty")}
    module = _load_collector(
        supervisor_log_dir=log_dir,
        mngr_binary=_mngr_stub_for_chats(tmp_path, chats),
        supervisord_conf=conf,
        workspace_dir=tmp_path / "workspace",
        scan_gate_dir=tmp_path / "absent-gate",
    )

    module.main(["--logs", "--transcript"])

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "contract_version": 1,
        "omissions": {
            "workspace_logs": "scanner_unavailable",
            "transcript": "scanner_unavailable",
        },
    }
    assert "zip" not in payload


# --- The size cap ---


def test_main_packs_every_collected_chat_with_no_size_cap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing is trimmed to fit: every chat that was collected and scanned clean
    is packed.

    The payload returns on stdout, which carries it whole, so there is no
    transport ceiling to stay under. A collection that grows past what the
    host's budget allows fails loudly as a timeout instead of quietly shipping a
    subset a reader could not tell from the complete set.
    """
    gate = tmp_path / "gate"
    _write_stub_scan_gate(gate, exit_code=0)
    # Hex of random bytes barely deflates, so each chat stays ~2KB in the zip.
    chats = {
        agent_id: _chat_events(agent_id, age_seconds=age)
        + json.dumps({"blob": os.urandom(2048).hex()})
        + "\n"
        for agent_id, age in (("agent-a", 180), ("agent-b", 120), ("agent-c", 60))
    }
    module = _load_collector(
        mngr_binary=_mngr_stub_for_chats(tmp_path, chats), scan_gate_dir=gate
    )

    module.main(["--transcript"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["omissions"] == {}
    with _zip_from_payload(payload) as archive:
        assert archive.namelist() == [
            "chats/agent-c-claude.jsonl",
            "chats/agent-b-claude.jsonl",
            "chats/agent-a-claude.jsonl",
        ]
