import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

NOW = datetime(2026, 10, 22, 12, 0, 30, tzinfo=timezone.utc)


def model_call_payload(**overrides: Any) -> dict[str, Any]:
    """A valid raw model_call event; ``attribution`` overrides merge into the default one."""
    attribution = {
        "workspace_id": "ws-1",
        "origin_kind": "APP",
        "origin_id": "files",
        **overrides.pop("attribution", {}),
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "event_id": uuid4().hex,
        "started_at": (NOW - timedelta(seconds=2)).isoformat(),
        "ended_at": NOW.isoformat(),
        "provider": "anthropic",
        "model": "anthropic/claude-opus-5-5",
        "tokens": {"input": 1000, "output": 100},
        "attribution": attribution,
    }
    payload.update(overrides)
    return payload


def write_claude_session(
    config_dir: Path, session_id: str, request_ids: list[str]
) -> Path:
    """A Claude Code session log under ``config_dir`` with one call per request id."""
    log = config_dir / "projects" / "-tmp-one-shot" / f"{session_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for index, request_id in enumerate(request_ids):
        lines.append(
            {
                "type": "user",
                "sessionId": session_id,
                "timestamp": f"2026-10-22T12:00:{index * 2:02d}Z",
            }
        )
        lines.append(
            {
                "type": "assistant",
                "requestId": request_id,
                "sessionId": session_id,
                "timestamp": f"2026-10-22T12:00:{index * 2 + 1:02d}Z",
                "message": {
                    "model": "claude-haiku-4-5-20251001",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 65,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 5812,
                        "cache_creation": {
                            "ephemeral_1h_input_tokens": 5812,
                            "ephemeral_5m_input_tokens": 0,
                        },
                    },
                },
            }
        )
    log.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return log
