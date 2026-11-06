"""Tests for report_quick_change.py: the quick-change agent's report, sent to the chat app or written to the log."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parent / "report_quick_change.py"


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_report_the_chat_app_cannot_take_is_appended_to_the_log(
    tmp_path: Path,
) -> None:
    apps_file = tmp_path / "apps.toml"
    apps_file.write_text(
        f'[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:{_unused_port()}"\n',
        encoding="utf-8",
    )
    log_path = tmp_path / "quick_changes.jsonl"

    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "req-1",
            "done",
            "Todo: header is dark blue.",
            "--log",
            str(log_path),
        ],
        env={"MINDS_APPS_FILE": str(apps_file), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    [record] = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert (
        record["type"],
        record["request_id"],
        record["status"],
        record["report"],
    ) == (
        "report",
        "req-1",
        "done",
        "Todo: header is dark blue.",
    )
