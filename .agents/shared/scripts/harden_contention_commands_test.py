"""The commands harden-contention.md gives a lead, run as written against a real ticket."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOC = _REPO_ROOT / ".agents" / "shared" / "references" / "harden-contention.md"
_TK = _REPO_ROOT / "system" / "vendor" / "tk" / "ticket"
_BASH_BLOCK = re.compile(r"```bash\n(.*?)```", re.DOTALL)


def _bash_block_containing(marker: str) -> str:
    blocks = [
        block for block in _BASH_BLOCK.findall(_DOC.read_text()) if marker in block
    ]
    assert len(blocks) == 1, f"expected one bash block holding {marker!r} in {_DOC}"
    return blocks[0]


def _run_bash(
    script: str, env: dict[str, str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="the workspace's sed is GNU sed")
def test_a_creation_a_catch_up_adds_is_found_by_the_single_flight_check(
    tmp_path: Path,
) -> None:
    """A lead that widens its pass records the added creation in its task file and its
    ticket's title, so another chat's single-flight check finds the pass by that creation and
    a chat taking the pass over leads it with every creation it now covers."""
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    (bin_directory / "tk").symlink_to(_TK)
    tickets_directory = tmp_path / "tickets"
    tickets_directory.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
        "TICKETS_DIR": str(tickets_directory),
    }
    env.pop("MNGR_AGENT_NAME", None)
    created = _run_bash(
        'tk create "update skill:text-stats" -t task -d "Worker: update-text-stats"',
        env,
        tmp_path,
    )
    assert created.returncode == 0, created.stderr
    ticket_id = created.stdout.strip()
    task_file = tmp_path / "task.md"
    task_file.write_text(
        "---\noperation: update\ncreations: skill:text-stats\n---\n\n# Task\n"
    )
    widen = (
        _bash_block_containing("ADDED=")
        .replace('"<each added creation, as type:name>"', '"skill:slug-title"')
        .replace("<TASK_FILE>", str(task_file))
        .replace("<ticket-id>", ticket_id)
    )
    single_flight = _bash_block_containing("tk ready > ").replace(
        "/tmp/harden-inflight.txt", str(tmp_path / "inflight.txt")
    )

    widened = _run_bash(widen, env, tmp_path)
    found = _run_bash(single_flight, {**env, "CREATIONS": "skill:slug-title"}, tmp_path)

    assert widened.returncode == 0, widened.stderr
    assert ticket_id in found.stdout
    assert "creations: skill:text-stats skill:slug-title\n" in task_file.read_text()


_WORKER_CREATED = "2026-05-01T12:00:00.123456Z"


def _run_takeover_lookup(
    tmp_path: Path, listing: str | None
) -> subprocess.CompletedProcess[str]:
    """Run the takeover's last-report lookup as written, against an ``mngr list`` that prints
    ``listing``, or fails when it is None."""
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    fake_mngr = bin_directory / "mngr"
    if listing is None:
        fake_mngr.write_text("#!/bin/sh\nexit 1\n")
    else:
        listing_file = tmp_path / "listing.jsonl"
        listing_file.write_text(listing)
        fake_mngr.write_text(f"#!/bin/sh\ncat '{listing_file}'\n")
    fake_mngr.chmod(0o755)
    lookup = (
        _bash_block_containing("-newermt")
        .replace("<worker-name>", "harden-notes")
        .replace("<finish_report_path>", str(tmp_path / "reports" / "report.md"))
        .replace("/tmp/harden-agents.jsonl", str(tmp_path / "agents.jsonl"))
    )
    env = {**os.environ, "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}"}
    return _run_bash(lookup, env, tmp_path)


@pytest.mark.skipif(sys.platform != "linux", reason="the workspace's find is GNU find")
@pytest.mark.skipif(
    shutil.which("jq") is None, reason="the lookup reads the agent list with jq"
)
@pytest.mark.parametrize(
    ("reports", "last_report"),
    [
        (
            {"earlier-pass-done.md": -3600, "1-gate.md": 60, "2-done.md": 120},
            "2-done.md",
        ),
        ({"earlier-pass-done.md": -3600}, None),
    ],
)
def test_a_taking_over_lead_finds_the_newest_report_its_worker_sent(
    tmp_path: Path, reports: dict[str, int], last_report: str | None
) -> None:
    """The last report is the newest one in consumed/ that arrived after the worker was made,
    so a report an earlier pass under the same name left there is never the one acted on."""
    consumed = tmp_path / "reports" / "consumed"
    consumed.mkdir(parents=True)
    created = datetime.fromisoformat(_WORKER_CREATED).timestamp()
    for name, seconds_after_created in reports.items():
        report = consumed / name
        report.write_text("report\n")
        os.utime(
            report, (created + seconds_after_created, created + seconds_after_created)
        )
    listing = (
        '{"name": "chat-1", "create_time": "2026-01-01T00:00:00Z"}\n'
        f'{{"name": "harden-notes", "create_time": "{_WORKER_CREATED}"}}\n'
    )

    result = _run_takeover_lookup(tmp_path, listing)

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "" if last_report is None else f"{consumed / last_report}\n"
    )


@pytest.mark.skipif(sys.platform != "linux", reason="the workspace's find is GNU find")
@pytest.mark.skipif(
    shutil.which("jq") is None, reason="the lookup reads the agent list with jq"
)
@pytest.mark.parametrize(
    ("listing", "exit_code"),
    [
        ('{"name": "chat-1", "create_time": "2026-01-01T00:00:00Z"}\n', 1),
        (None, 2),
    ],
)
def test_a_takeover_lookup_tells_a_gone_worker_from_an_unread_agent_list(
    tmp_path: Path, listing: str | None, exit_code: int
) -> None:
    """No agent by the worker's name means the worker is gone, and the pass is superseded; an
    agent list that could not be read says nothing about the worker, so it exits differently."""
    (tmp_path / "reports" / "consumed").mkdir(parents=True)

    result = _run_takeover_lookup(tmp_path, listing)

    assert result.returncode == exit_code
    assert result.stdout == ""
