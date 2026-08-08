"""Unit tests for the Claude model-state hook's snapshot computation."""

import json
from pathlib import Path

from imbue.mngr_claude.resources import model_state_hook


def _managed_path(state_dir: Path) -> Path:
    path = state_dir.joinpath(*model_state_hook._MANAGED_SETTINGS_RELPATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _hook_json(event: str, transcript_path: str = "") -> str:
    return json.dumps({"hook_event_name": event, "transcript_path": transcript_path})


def test_no_state_dir_writes_nothing() -> None:
    assert model_state_hook.compute_snapshot({}, _hook_json("Stop")) is None


def test_before_a_turn_uses_settings_preference(tmp_path: Path) -> None:
    # SessionStart / UserPromptSubmit: no fresh assistant message, so model + fast come from
    # settings (the optimistic preference), effort from settings.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(json.dumps({"model": "sonnet", "effortLevel": "high", "fastMode": True}))
    env = {"MNGR_AGENT_STATE_DIR": str(state_dir), "CLAUDE_CONFIG_DIR": str(config_dir)}

    snapshot = model_state_hook.compute_snapshot(env, _hook_json("UserPromptSubmit"))
    assert snapshot == {"model": "sonnet", "effort": "high", "fast": True}


def test_managed_fast_mode_wins_over_user_settings(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(json.dumps({"model": "opus[1m]", "fastMode": True}))
    _managed_path(state_dir).write_text(json.dumps({"fastMode": False}))
    env = {"MNGR_AGENT_STATE_DIR": str(state_dir), "CLAUDE_CONFIG_DIR": str(config_dir)}

    snapshot = model_state_hook.compute_snapshot(env, _hook_json("SessionStart"))
    assert snapshot is not None
    assert snapshot["fast"] is False


def test_stop_reads_effective_model_and_fast_from_transcript(tmp_path: Path) -> None:
    # At Stop the last assistant message is the truth: the raw model id it actually ran, and
    # service_tier "standard" means fast did NOT run even though settings say fast-on.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps({"model": "opus[1m]", "effortLevel": "max", "fastMode": True})
    )
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "model": "claude-opus-4-8", "usage": {"service_tier": "standard"}},
            }
        )
        + "\n"
    )
    env = {"MNGR_AGENT_STATE_DIR": str(state_dir), "CLAUDE_CONFIG_DIR": str(config_dir)}

    snapshot = model_state_hook.compute_snapshot(env, _hook_json("Stop", str(transcript)))
    # Model + fast from the transcript (fast off -- standard tier); effort still from settings.
    assert snapshot == {"model": "claude-opus-4-8", "effort": "max", "fast": False}


def test_stop_marks_fast_when_service_tier_is_priority(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "model": "claude-opus-4-8", "usage": {"service_tier": "priority"}},
            }
        )
        + "\n"
    )
    env = {"MNGR_AGENT_STATE_DIR": str(state_dir)}

    snapshot = model_state_hook.compute_snapshot(env, _hook_json("Stop", str(transcript)))
    assert snapshot is not None
    assert snapshot["fast"] is True


def test_post_tool_use_reads_transcript_mid_turn(tmp_path: Path) -> None:
    # Firing after a tool call must also correct from the transcript, so the bar updates
    # within the turn rather than only at Stop.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps({"model": "opus[1m]", "effortLevel": "high", "fastMode": True})
    )
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "model": "claude-sonnet-4-5", "usage": {"service_tier": "standard"}},
            }
        )
        + "\n"
    )
    env = {"MNGR_AGENT_STATE_DIR": str(state_dir), "CLAUDE_CONFIG_DIR": str(config_dir)}

    snapshot = model_state_hook.compute_snapshot(env, _hook_json("PostToolUse", str(transcript)))
    assert snapshot == {"model": "claude-sonnet-4-5", "effort": "high", "fast": False}


def test_last_assistant_skips_user_and_malformed_lines(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-4-8"}})
        + "\n"
        + "not json\n"
        + json.dumps({"type": "user", "message": {"role": "user", "content": "x"}})
        + "\n"
    )
    message = model_state_hook._last_assistant(str(transcript))
    assert message is not None
    assert message["model"] == "claude-opus-4-8"


def test_last_assistant_none_when_no_transcript() -> None:
    assert model_state_hook._last_assistant("") is None
    assert model_state_hook._last_assistant("/no/such/file.jsonl") is None
