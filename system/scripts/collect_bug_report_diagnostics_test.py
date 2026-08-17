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
import zipfile
from collections.abc import Sequence
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
    mngr_home_dirs: Sequence[Path] | None = None,
    supervisord_conf: Path | None = None,
    workspace_dir: Path | None = None,
    scan_gate_dir: Path | None = None,
    max_encoded_zip_bytes: int | None = None,
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
    if mngr_home_dirs is not None:
        _rebind(
            namespace, "MNGR_HOME_CANDIDATES", [str(path) for path in mngr_home_dirs]
        )
    if supervisord_conf is not None:
        _rebind(namespace, "SUPERVISORD_CONF", str(supervisord_conf))
    if workspace_dir is not None:
        _rebind(namespace, "WORKSPACE_DIR", str(workspace_dir))
    if scan_gate_dir is not None:
        _rebind(namespace, "SCAN_GATE_DIR", str(scan_gate_dir))
    if max_encoded_zip_bytes is not None:
        _rebind(namespace, "MAX_ENCODED_ZIP_BYTES", max_encoded_zip_bytes)
    return module


def _write_log(log_dir: Path, name: str, *, mtime: float, content: str = "") -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_text(content, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _write_transcript(
    mngr_home: Path,
    agent_id: str,
    *,
    mtime: float,
    content: str,
    source: str = "claude",
    labels: dict[str, str] | None = None,
) -> Path:
    path = (
        mngr_home
        / "agents"
        / agent_id
        / "events"
        / source
        / "common_transcript"
        / "events.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    if labels is not None:
        (mngr_home / "agents" / agent_id / "data.json").write_text(
            json.dumps({"labels": labels}), encoding="utf-8"
        )
    return path


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


def test_collector_caps_match_the_documented_limits() -> None:
    module = _load_collector()
    assert module.CONTRACT_VERSION == 1
    assert module.MAX_LOG_FILES == 100
    assert module.MAX_LINES_PER_LOG == 200
    assert module.MAX_ENCODED_ZIP_BYTES == 8 * 1024 * 1024


def test_select_log_files_caps_at_the_newest_hundred_files(tmp_path: Path) -> None:
    log_dir = tmp_path / "supervisor"
    over_cap_count = 120
    for index in range(over_cap_count):
        _write_log(log_dir, f"svc-{index:03d}-stderr.log", mtime=1_000_000 + index)
    module = _load_collector(supervisor_log_dir=log_dir)

    selected = module.select_log_files(None)

    assert len(selected) == module.MAX_LOG_FILES
    expected_newest_first = [
        str(log_dir / f"svc-{index:03d}-stderr.log")
        for index in range(
            over_cap_count - 1, over_cap_count - 1 - module.MAX_LOG_FILES, -1
        )
    ]
    assert selected == expected_newest_first


# --- User-app log exclusion ---


def test_load_user_program_names_finds_only_the_programs_tagged_with_the_user_band(
    tmp_path: Path,
) -> None:
    """The literal ``user`` band argument is how the workspace marks its own apps.

    ``xvfb`` tags itself by name and ``cron`` has no wrapper; neither is a user
    app, though both would be misread as one by a band-table lookup.
    """
    conf = tmp_path / "supervisord.conf"
    conf.write_text(_FIXTURE_SUPERVISORD_CONF, encoding="utf-8")
    module = _load_collector(supervisord_conf=conf)

    assert module.load_user_program_names() == {"geopolitical-dashboard"}


def test_load_user_program_names_is_none_when_the_config_cannot_be_read(
    tmp_path: Path,
) -> None:
    module = _load_collector(supervisord_conf=tmp_path / "no-such-supervisord.conf")

    assert module.load_user_program_names() is None


def test_select_log_files_drops_user_app_logs_and_keeps_system_ones(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "supervisor"
    _write_log(log_dir, "system_interface-stderr.log", mtime=1_000_004)
    _write_log(log_dir, "system_interface-stdout.log", mtime=1_000_003)
    _write_log(log_dir, "terminal-stderr.log", mtime=1_000_002)
    _write_log(log_dir, "geopolitical-dashboard-stderr.log", mtime=1_000_001)
    # Only system_interface's stdout is collected; every other program
    # contributes stderr alone.
    _write_log(log_dir, "terminal-stdout.log", mtime=1_000_005)
    module = _load_collector(supervisor_log_dir=log_dir)

    selected = module.select_log_files({"geopolitical-dashboard"})

    assert [os.path.basename(path) for path in selected] == [
        "system_interface-stderr.log",
        "system_interface-stdout.log",
        "terminal-stderr.log",
    ]


def test_select_log_files_keeps_every_log_when_the_classification_is_unavailable(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "supervisor"
    _write_log(log_dir, "system_interface-stderr.log", mtime=1_000_001)
    _write_log(log_dir, "geopolitical-dashboard-stderr.log", mtime=1_000_000)
    module = _load_collector(supervisor_log_dir=log_dir)

    selected = module.select_log_files(None)

    assert [os.path.basename(path) for path in selected] == [
        "system_interface-stderr.log",
        "geopolitical-dashboard-stderr.log",
    ]


# --- Chat selection ---


def test_select_transcript_paths_includes_every_chat_written_to_inside_the_window(
    tmp_path: Path,
) -> None:
    """A bug is rarely about exactly one conversation: every recently written chat
    rides along, newest first, while chats idle past the window stay out."""
    now = time.time()
    mngr_home = tmp_path / "mngr-home"
    older_recent = _write_transcript(
        mngr_home, "agent-a", mtime=now - 3_000, content='{"seq": 1}\n'
    )
    newest_recent = _write_transcript(
        mngr_home, "agent-b", mtime=now - 60, content='{"seq": 2}\n'
    )
    _write_transcript(
        mngr_home, "agent-idle", mtime=now - 10_000, content='{"seq": 3}\n'
    )
    # The first candidate root is one that does not exist in this layout,
    # mirroring how the script probes several homes in order.
    module = _load_collector(mngr_home_dirs=(tmp_path / "absent-home", mngr_home))

    assert module.select_transcript_paths() == [str(newest_recent), str(older_recent)]


def test_select_transcript_paths_prefers_the_chat_the_user_last_wrote_in(
    tmp_path: Path,
) -> None:
    """Fallback path (no chat inside the recency window): when the user last spoke
    beats the file mtime, since a background chat's assistant can keep appending
    (and bumping the mtime) long after the user moved on."""
    mngr_home = tmp_path / "mngr-home"
    _write_transcript(
        mngr_home,
        "agent-churning",
        mtime=2_000_000,
        content=_user_message_line("2026-08-01T10:00:00.000Z"),
    )
    quiet = _write_transcript(
        mngr_home,
        "agent-quiet",
        mtime=1_000_000,
        content=_user_message_line("2026-08-02T10:00:00.000Z"),
    )
    module = _load_collector(mngr_home_dirs=(mngr_home,))

    assert module.select_transcript_paths() == [str(quiet)]


def test_select_transcript_paths_skips_background_agents(tmp_path: Path) -> None:
    """Workers and the services agent are the workspace's own background marks.

    Mirrors system_interface's chat definition: ``agent_created=true`` is a
    worker another agent spawned, ``is_primary=true`` the services agent. Both
    can hold newer activity than the chat the user was actually in.
    """
    mngr_home = tmp_path / "mngr-home"
    _write_transcript(
        mngr_home,
        "agent-worker",
        mtime=3_000_000,
        content=_user_message_line("2026-08-03T10:00:00.000Z"),
        labels={"agent_created": "true"},
    )
    _write_transcript(
        mngr_home,
        "agent-services",
        mtime=3_000_000,
        content=_user_message_line("2026-08-03T10:00:00.000Z"),
        labels={"is_primary": "true"},
    )
    chat = _write_transcript(
        mngr_home,
        "agent-chat",
        mtime=1_000_000,
        content=_user_message_line("2026-08-01T10:00:00.000Z"),
        labels={"user_created": "true"},
    )
    module = _load_collector(mngr_home_dirs=(mngr_home,))

    assert module.select_transcript_paths() == [str(chat)]


def test_select_transcript_paths_ignores_the_converters_own_log_stream(
    tmp_path: Path,
) -> None:
    """``events/logs/common_transcript`` records that a conversion happened, not
    what was said, and is always newer than the transcript it just wrote."""
    mngr_home = tmp_path / "mngr-home"
    conversation = _write_transcript(
        mngr_home, "agent-chat", mtime=1_000_000, content='{"role": "user"}\n'
    )
    _write_transcript(
        mngr_home,
        "agent-chat",
        mtime=2_000_000,
        content='{"message": "Converted 3 new event(s)"}\n',
        source="logs",
    )
    module = _load_collector(mngr_home_dirs=(mngr_home,))

    assert module.select_transcript_paths() == [str(conversation)]


def test_select_transcript_paths_is_empty_when_no_agent_has_a_transcript(
    tmp_path: Path,
) -> None:
    mngr_home = tmp_path / "mngr-home"
    (mngr_home / "agents" / "agent-empty" / "events").mkdir(parents=True)
    module = _load_collector(mngr_home_dirs=(mngr_home,))

    assert module.select_transcript_paths() == []


# --- Member naming ---


def test_transcript_member_names_live_under_chats_and_cannot_escape_an_extraction_directory() -> (
    None
):
    """Member names are built from directory names inside the workspace, so they
    are sanitized before they can reach a reader's filesystem on extraction."""
    module = _load_collector()
    traversing_path = (
        "/mngr/agents/../../../etc/events/claude/common_transcript/events.jsonl"
    )

    name = module.transcript_member_name(traversing_path, set())

    assert name.startswith("chats/"), name
    remainder = name[len("chats/") :]
    assert "/" not in remainder, name
    assert ".." not in remainder, name
    assert not remainder.startswith("."), name


def test_transcript_member_names_stay_unique_within_one_archive() -> None:
    """Two chats that resolve to the same agent and harness must not collide: a
    zip member silently overwriting another would drop a whole conversation."""
    module = _load_collector()
    path = "/mngr/agents/agent-a/events/claude/common_transcript/events.jsonl"
    used: set[str] = set()

    first = module.transcript_member_name(path, used)
    second = module.transcript_member_name(path, used)

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
    mngr_home = tmp_path / "mngr-home"
    _write_transcript(
        mngr_home,
        "agent-older",
        mtime=now - 600,
        content='{"seq": "older"}\n',
        source="claude",
    )
    _write_transcript(
        mngr_home,
        "agent-newer",
        mtime=now - 60,
        content='{"seq": "newer"}\n',
        source="codex",
    )
    module = _load_collector(
        supervisor_log_dir=log_dir,
        mngr_home_dirs=(mngr_home,),
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
            "workspace-logs.log",
            "chats/agent-newer-codex.jsonl",
            "chats/agent-older-claude.jsonl",
        ]
        logs = archive.read("workspace-logs.log").decode("utf-8")
        assert "interface started" in logs
        assert "=== workspace version ===" in logs
        assert (
            archive.read("chats/agent-newer-codex.jsonl").decode("utf-8")
            == '{"seq": "newer"}\n'
        )
        assert (
            archive.read("chats/agent-older-claude.jsonl").decode("utf-8")
            == '{"seq": "older"}\n'
        )
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
    mngr_home = tmp_path / "mngr-home"
    mngr_home.mkdir()
    module = _load_collector(mngr_home_dirs=(mngr_home,))

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
    mngr_home = tmp_path / "mngr-home"
    _write_transcript(
        mngr_home, "agent-chat", mtime=time.time() - 60, content='{"seq": 1}\n'
    )
    module = _load_collector(
        supervisor_log_dir=log_dir,
        mngr_home_dirs=(mngr_home,),
        supervisord_conf=conf,
        workspace_dir=tmp_path / "workspace",
        scan_gate_dir=gate,
    )

    module.main(["--logs"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["omissions"] == {}
    with _zip_from_payload(payload) as archive:
        assert archive.namelist() == ["workspace-logs.log"]


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
    now = time.time()
    mngr_home = tmp_path / "mngr-home"
    _write_transcript(
        mngr_home, "agent-clean", mtime=now - 60, content='{"seq": "clean"}\n'
    )
    _write_transcript(
        mngr_home,
        "agent-dirty",
        mtime=now - 600,
        content=json.dumps({"key": secret}) + "\n",
    )
    module = _load_collector(mngr_home_dirs=(mngr_home,), scan_gate_dir=gate)

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
    mngr_home = tmp_path / "mngr-home"
    _write_transcript(
        mngr_home,
        f"agent-{secret}",
        mtime=time.time() - 60,
        content='{"seq": "clean"}\n',
    )
    module = _load_collector(mngr_home_dirs=(mngr_home,), scan_gate_dir=gate)

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
    mngr_home = tmp_path / "mngr-home"
    _write_transcript(
        mngr_home, "agent-clean", mtime=time.time() - 60, content='{"seq": "clean"}\n'
    )
    module = _load_collector(
        supervisor_log_dir=log_dir,
        mngr_home_dirs=(mngr_home,),
        supervisord_conf=conf,
        workspace_dir=tmp_path / "workspace",
        scan_gate_dir=gate,
    )

    module.main(["--logs", "--transcript"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["omissions"] == {"workspace_logs": "secrets_found"}
    with _zip_from_payload(payload) as archive:
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
    mngr_home = tmp_path / "mngr-home"
    _write_transcript(
        mngr_home, "agent-chat", mtime=time.time() - 60, content='{"seq": 1}\n'
    )
    module = _load_collector(
        supervisor_log_dir=log_dir,
        mngr_home_dirs=(mngr_home,),
        supervisord_conf=conf,
        workspace_dir=tmp_path / "workspace",
        scan_gate_dir=tmp_path / "no-such-gate",
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


def test_main_drops_oldest_chats_first_when_the_payload_exceeds_the_cap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Over the cap, the oldest chats go first and the drop is recorded inside
    the zip -- a silently trimmed set would be indistinguishable from a complete
    one. The cap constant is rebound small so the fixture chats stay small; the
    real value is pinned by test_collector_caps_match_the_documented_limits.
    """
    gate = tmp_path / "gate"
    _write_stub_scan_gate(gate, exit_code=0)
    now = time.time()
    mngr_home = tmp_path / "mngr-home"
    # Hex of random bytes barely deflates, so each chat stays ~2KB in the zip.
    for agent_id, age in (("agent-a", 180), ("agent-b", 120), ("agent-c", 60)):
        content = json.dumps({"blob": os.urandom(2048).hex()}) + "\n"
        _write_transcript(mngr_home, agent_id, mtime=now - age, content=content)
    module = _load_collector(
        mngr_home_dirs=(mngr_home,), scan_gate_dir=gate, max_encoded_zip_bytes=5_000
    )

    module.main(["--transcript"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["omissions"] == {}
    with _zip_from_payload(payload) as archive:
        assert archive.namelist() == ["chats/agent-c-claude.jsonl", "DROPPED-CHATS.txt"]
        note = archive.read("DROPPED-CHATS.txt").decode("utf-8")
        # Oldest dropped first, and both drops are on the record.
        assert note.index("chats/agent-a-claude.jsonl") < note.index(
            "chats/agent-b-claude.jsonl"
        )
    assert len(payload["zip"]) <= 5_000
