"""grok's Stop filter runs a stop gate at a turn's end and nothing when a session ends."""

import json
import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent / "grok_turn_end_stop_hook.sh"
# Records the stdin it gets in the file its first argument names, then blocks as a stop gate does.
_RECORDING_GATE = ("bash", "-c", 'cat > "$0"; exit 2')


def _run_filter(payload: str, received_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), *_RECORDING_GATE, str(received_path)],
        input=payload,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=30,
    )


def test_a_turn_end_runs_the_gate_on_the_payload_and_keeps_its_exit_status(tmp_path: Path) -> None:
    payload = {"hook_event_name": "Stop", "session_id": "s-1", "reason": "end_turn"}
    received_path = tmp_path / "received.json"

    completed = _run_filter(json.dumps(payload), received_path)

    assert completed.returncode == 2
    assert json.loads(received_path.read_text()) == payload


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(json.dumps({"hook_event_name": "Stop", "reason": "shutdown"}), id="session-end"),
        pytest.param(json.dumps({"hook_event_name": "Stop"}), id="no-reason"),
        pytest.param("not json", id="unreadable"),
    ],
)
def test_a_stop_that_ends_no_turn_runs_nothing(tmp_path: Path, payload: str) -> None:
    received_path = tmp_path / "received.json"

    completed = _run_filter(payload, received_path)

    assert completed.returncode == 0
    assert not received_path.exists()
