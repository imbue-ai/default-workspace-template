import json
from pathlib import Path

import pytest
from env_converge.events import EnvConvergeEventType, default_events_path, emit_event


def test_emit_event_appends_a_parseable_envelope_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The env.d unit runner emits through this path on every boot; a
    # serialization failure here aborts the whole slow phase, so the write
    # must survive with a real MNGR_HOST_DIR set (unset skips emission
    # entirely, which is why tests without it never exercise the write).
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))

    emit_event(EnvConvergeEventType.UNIT_RUN, {"unit": "1000-playwright-fortress.sh"})

    events_path = default_events_path()
    assert events_path is not None
    lines = events_path.read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "unit_run"
    assert event["source"] == "env_converge"
    assert event["detail"] == {"unit": "1000-playwright-fortress.sh"}
    assert event["event_id"].startswith("evc-")
    assert event["timestamp"]


def test_emit_event_is_a_noop_without_a_host_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    emit_event(EnvConvergeEventType.UNIT_RUN, {"unit": "x"})

    assert list(tmp_path.rglob("events.jsonl")) == []
