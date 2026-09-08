"""Acceptance test: the injected collection script end to end, exactly as injected.

Lays the script files out with the runner's own injection map
(``collection.load_injected_script_files``), then executes ``uv run --script
collect.py`` as a real subprocess -- resolving the script's PEP 723
environment (pinned Presidio + spacy model; network on first run, cached
after) -- against a fixture workspace, and validates the stdout with the
runner's protocol parser. Asserts the honest-collection invariants: real PII
scrubbing, tool outputs dropped, in-workspace audit artifacts written, and an
idempotent re-run from the advanced cursors.

The secret scanners are faked on PATH (their report formats are covered by
unit tests); everything else is the production code path.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from imbue.analytics.collection import compute_script_version
from imbue.analytics.collection import load_injected_script_files
from imbue.analytics.protocol import parse_collection_output

_UV_RUN_TIMEOUT_SECONDS = 540

_FAKE_CLEAN_BETTERLEAKS = """#!/bin/sh
report_path=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --report-path) report_path="$2"; shift 2 ;;
        *) shift ;;
    esac
done
printf '[]' > "$report_path"
exit 0
"""

_FAKE_CLEAN_KINGFISHER = """#!/bin/sh
exit 0
"""


def _write_fixture_workspace(base_dir: Path) -> tuple[Path, Path]:
    workspace_root = base_dir / "workspace"
    host_dir = base_dir / ".mngr"
    workspace_root.mkdir()
    transcript_file = host_dir / "agents/agent-e2e/events/claude/common_transcript/events.jsonl"
    transcript_file.parent.mkdir(parents=True)
    transcript_records = [
        {
            "timestamp": "2026-08-18T12:00:00.000000000Z",
            "event_id": "evt-user-1",
            "type": "user_message",
            "source": "claude",
            "content": "please email results to josh.tester@example.com today",
        },
        {
            "timestamp": "2026-08-18T12:00:05.000000000Z",
            "event_id": "evt-tool-1",
            "type": "tool_result",
            "source": "claude",
            "tool_call_id": "call-1",
            "tool_name": "Read",
            "output": "SUPER-SENSITIVE-FILE-CONTENTS-61834",
            "is_error": False,
        },
    ]
    transcript_file.write_text("".join(json.dumps(record) + "\n" for record in transcript_records))
    # A second agent on the same workspace, provisioned after the ATIF cutover:
    # both stream vintages are collected from the same fleet.
    atif_file = host_dir / "agents/agent-atif/events/claude/common_transcript/events.jsonl"
    atif_file.parent.mkdir(parents=True)
    atif_records = [
        {
            "type": "header",
            "event_id": "header",
            "emitter": "claude/common_transcript",
            "schema_version": "ATIF-v1.7",
        },
        {
            "type": "step",
            "event_id": "evt-step-1",
            "emitter": "claude/common_transcript",
            "timestamp": "2026-08-18T12:01:00.000000000Z",
            "source": "user",
            "message": "please email results to josh.tester@example.com today",
        },
        {
            "type": "step",
            "event_id": "evt-step-2",
            "emitter": "claude/common_transcript",
            "timestamp": "2026-08-18T12:01:05.000000000Z",
            "source": "agent",
            "message": "reading it now",
            "reasoning_content": "SUPER-SENSITIVE-REASONING-90210",
            "model_name": "claude-fable-5",
            "tool_calls": [
                {
                    "tool_call_id": "call-atif-1",
                    "function_name": "Read",
                    "arguments": {"file_path": "/SUPER-SENSITIVE-ARGUMENT-44771"},
                }
            ],
        },
        {
            "type": "observation",
            "event_id": "evt-obs-1",
            "emitter": "claude/common_transcript",
            "timestamp": "2026-08-18T12:01:06.000000000Z",
            "results": [
                {
                    "source_call_id": "call-atif-1",
                    "content": "SUPER-SENSITIVE-FILE-CONTENTS-61834",
                    "extra": {"is_error": False, "tool_name": "Read"},
                }
            ],
        },
    ]
    atif_file.write_text("".join(json.dumps(record) + "\n" for record in atif_records))
    return workspace_root, host_dir


def _install_fake_scanners(base_dir: Path) -> Path:
    bin_dir = base_dir / "fake-bin"
    bin_dir.mkdir()
    for name, body in (("betterleaks", _FAKE_CLEAN_BETTERLEAKS), ("kingfisher", _FAKE_CLEAN_KINGFISHER)):
        script = bin_dir / name
        script.write_text(body)
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def _inject_script_files(analytics_dir: Path) -> None:
    for remote_relpath, content in load_injected_script_files().items():
        target = analytics_dir / remote_relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def _run_collect(
    analytics_dir: Path, workspace_root: Path, host_dir: Path, scanners_bin: Path, run_id: str
) -> subprocess.CompletedProcess[str]:
    command = [
        "uv",
        "run",
        "--script",
        str(analytics_dir / "collect.py"),
        "--run-id",
        run_id,
        "--script-version",
        compute_script_version(load_injected_script_files()),
        "--workspace-root",
        str(workspace_root),
        "--host-dir",
        str(host_dir),
        "--cursors-file",
        str(analytics_dir / "cursors.json"),
        "--budget-bytes",
        str(64 * 1024 * 1024),
    ]
    environment = {**os.environ, "PATH": f"{scanners_bin}{os.pathsep}{os.environ.get('PATH', '')}"}
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=_UV_RUN_TIMEOUT_SECONDS,
        env=environment,
        cwd=str(analytics_dir),
    )


@pytest.mark.acceptance
@pytest.mark.timeout(600)
def test_injected_script_collects_and_redacts_end_to_end(tmp_path: Path) -> None:
    workspace_root, host_dir = _write_fixture_workspace(tmp_path)
    analytics_dir = workspace_root / "data" / ".imbue" / "analytics"
    _inject_script_files(analytics_dir)
    (analytics_dir / "cursors.json").write_text("{}")
    scanners_bin = _install_fake_scanners(tmp_path)

    first_run = _run_collect(analytics_dir, workspace_root, host_dir, scanners_bin, run_id="run-e2e-1")
    assert first_run.returncode == 0, f"collect.py failed: {first_run.stderr[-2000:]}"

    parsed = parse_collection_output(first_run.stdout)
    # The ATIF stream header is skipped rather than stored (it describes the stream, not an event
    # in it) and, being framing rather than corruption, is not counted as a dropped line.
    assert parsed.dropped_line_count == 0
    assert parsed.run_summary is not None

    # Real Presidio scrubbed the email; the sensitive tool output is gone
    # entirely (only its byte count survives).
    transcript_payloads = [json.loads(record.payload) for record in parsed.transcript_records]
    assert all(payload["type"] != "header" for payload in transcript_payloads)
    user_message = next(payload for payload in transcript_payloads if payload["type"] == "user_message")
    assert "josh.tester@example.com" not in user_message["content"]
    assert "[REDACTED_EMAIL_ADDRESS]" in user_message["content"]
    tool_result = next(payload for payload in transcript_payloads if payload["type"] == "tool_result")
    assert "output" not in tool_result
    assert tool_result["output_byte_count"] == len("SUPER-SENSITIVE-FILE-CONTENTS-61834")
    assert "SUPER-SENSITIVE-FILE-CONTENTS-61834" not in first_run.stdout

    # The same invariants on the ATIF-shaped stream: message text scrubbed,
    # reasoning and tool arguments gone, tool output reduced to a byte count.
    user_step = next(
        payload for payload in transcript_payloads if payload["type"] == "step" and payload["source"] == "user"
    )
    assert "josh.tester@example.com" not in user_step["message"]
    assert "[REDACTED_EMAIL_ADDRESS]" in user_step["message"]
    agent_step = next(
        payload for payload in transcript_payloads if payload["type"] == "step" and payload["source"] == "agent"
    )
    assert "reasoning_content" not in agent_step
    assert agent_step["tool_calls"] == [{"tool_call_id": "call-atif-1", "function_name": "Read"}]
    observation = next(payload for payload in transcript_payloads if payload["type"] == "observation")
    (result,) = observation["results"]
    assert "content" not in result
    assert result["content_byte_count"] == len("SUPER-SENSITIVE-FILE-CONTENTS-61834")
    assert result["extra"] == {"is_error": False, "tool_name": "Read"}
    assert "SUPER-SENSITIVE-REASONING-90210" not in first_run.stdout
    assert "SUPER-SENSITIVE-ARGUMENT-44771" not in first_run.stdout

    # In-workspace audit artifacts: the README seed and one collections.jsonl record.
    assert (analytics_dir / "README.md").is_file()
    audit_lines = (analytics_dir / "collections.jsonl").read_text().splitlines()
    assert len(audit_lines) == 1
    assert json.loads(audit_lines[0])["event_id"] == "run-e2e-1"

    # Idempotent re-run: persist the advanced cursors the way the runner does,
    # run again, and nothing is re-collected.
    new_cursors = {
        source: json.loads(cursor_value) for source, cursor_value in parsed.run_summary.cursor_by_source.items()
    }
    (analytics_dir / "cursors.json").write_text(json.dumps(new_cursors))
    second_run = _run_collect(analytics_dir, workspace_root, host_dir, scanners_bin, run_id="run-e2e-2")
    assert second_run.returncode == 0, f"re-run failed: {second_run.stderr[-2000:]}"
    second_parsed = parse_collection_output(second_run.stdout)
    assert second_parsed.transcript_records == ()
    assert len((analytics_dir / "collections.jsonl").read_text().splitlines()) == 2
