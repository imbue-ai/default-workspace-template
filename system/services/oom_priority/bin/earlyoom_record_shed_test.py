"""Tests for the earlyoom kill hook, run as earlyoom runs it: a bare ``python3``
subprocess with the victim in its environment."""

import json
import os
import subprocess
import sys
from pathlib import Path

from oom_priority.registry import record_agent_pid

_SCRIPT = Path(__file__).parent / "earlyoom_record_shed.py"


def _run_hook(runtime: Path, victim_env: dict[str, str]) -> list[dict]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("EARLYOOM_")
    }
    env.update(victim_env, OOM_PRIORITY_RUNTIME_DIR=str(runtime))
    subprocess.run([sys.executable, str(_SCRIPT)], env=env, check=True)
    ledger_path = runtime / "events" / "shed.jsonl"
    return [json.loads(line) for line in ledger_path.read_text().splitlines()]


def test_records_the_forks_victim_fields(tmp_path: Path) -> None:
    (record,) = _run_hook(
        tmp_path,
        {
            "EARLYOOM_PID": "4242",
            "EARLYOOM_UID": "0",
            "EARLYOOM_NAME": "pytest",
            "EARLYOOM_CMDLINE": "pytest -x",
            "EARLYOOM_OOM_SCORE_ADJ": "900",
            "EARLYOOM_BADNESS_KIB": "7390000",
            "EARLYOOM_VMRSS_KIB": "20000",
            "EARLYOOM_ORDERING": "kernel_badness",
        },
    )
    assert (record["pid"], record["comm"], record["agent_name"]) == (
        4242,
        "pytest",
        None,
    )
    assert (
        record["oom_score_adj"],
        record["badness_kib"],
        record["vm_rss_kib"],
        record["ordering"],
    ) == (900, 7390000, 20000, "kernel_badness")


def test_stock_earlyoom_victim_is_recorded_with_null_victim_fields(
    tmp_path: Path, monkeypatch
) -> None:
    # Stock earlyoom (what a workspace runs until it takes the update) passes
    # only the pid, uid, name and cmdline. The agent lookup still works.
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path))
    record_agent_pid(5151, "alpha", is_worker=True)

    (record,) = _run_hook(
        tmp_path,
        {
            "EARLYOOM_PID": "5151",
            "EARLYOOM_UID": "0",
            "EARLYOOM_NAME": "claude",
            "EARLYOOM_CMDLINE": "claude",
        },
    )
    assert (record["agent_name"], record["is_worker"]) == ("alpha", True)
    assert [
        record[key]
        for key in ("oom_score_adj", "badness_kib", "vm_rss_kib", "ordering")
    ] == [None, None, None, None]
