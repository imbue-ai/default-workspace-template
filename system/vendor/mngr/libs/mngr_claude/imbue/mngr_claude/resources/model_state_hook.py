#!/usr/bin/env python3
"""Write the agent's live model/effort/fast to a snapshot the chat model bar reads.

Claude's own settings only record the *preference*: when fast mode is unavailable at runtime
(usage credits exhausted) Claude silently runs standard and never writes that down, so
``settings.json`` says fast-on while the pane's lightning is off. The truth is per-turn in the
transcript -- each assistant message carries ``model`` and ``usage.service_tier`` (``"priority"``
when fast actually ran, else ``"standard"``). Effort is not in the transcript, so it comes from
settings.

This runs as a Claude Code hook (SessionStart, UserPromptSubmit, Stop) -- out of band, never in
the agent's context. It reads the hook JSON on stdin (``hook_event_name`` and, at Stop, the
exact ``transcript_path``) and writes ``$MNGR_AGENT_STATE_DIR/claude_model_state.json``
atomically. Best-effort: any failure exits 0 so a hook never breaks Claude's loop.

Kept in sync with the system-interface Claude resolver (harnesses/claude/model.py), which reads
this file: keys ``model`` (raw id or alias), ``effort`` (string or null), ``fast`` (bool).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

_STATE_NAME = "claude_model_state.json"
# Kept in sync with get_managed_settings_path in mngr_claude/claude_config.py.
_MANAGED_SETTINGS_RELPATH = ("plugin", "claude", "mngr_managed_settings.json")
_FAST_SERVICE_TIER = "priority"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _last_assistant(transcript_path: str) -> dict | None:
    """The last assistant message dict in the transcript JSONL, or None."""
    if not transcript_path:
        return None
    try:
        lines = Path(transcript_path).read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = record.get("message") if isinstance(record, dict) else None
        if isinstance(message, dict) and message.get("role") == "assistant" and message.get("model"):
            return message
    return None


def compute_snapshot(env: Mapping[str, str], hook_input_text: str) -> dict | None:
    """The ``{model, effort, fast}`` snapshot for this hook fire, or None to write nothing.

    Pure aside from reading the managed/user settings and transcript files named by ``env`` and
    the hook JSON -- no environment or stdin access -- so it is directly testable.
    """
    state_dir = env.get("MNGR_AGENT_STATE_DIR")
    if not state_dir:
        return None
    try:
        hook_input = json.loads(hook_input_text or "{}")
    except (json.JSONDecodeError, ValueError):
        hook_input = {}
    event = hook_input.get("hook_event_name") or ""
    transcript_path = hook_input.get("transcript_path") or ""

    state_path = Path(state_dir)
    managed = _read_json(state_path.joinpath(*_MANAGED_SETTINGS_RELPATH))
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    user_settings = _read_json(Path(config_dir) / "settings.json") if config_dir else {}

    # Effort is settings-only (not in the transcript): user settings win over managed.
    effort = user_settings.get("effortLevel")
    if not isinstance(effort, str) or not effort:
        managed_effort = managed.get("effortLevel")
        effort = managed_effort if isinstance(managed_effort, str) and managed_effort else None

    # Mid-turn (PostToolUse) and at Stop, the last assistant message is the effective truth: its
    # model (even if Claude auto-fell-back) and its service tier (the real fast state), so the
    # bar corrects within the turn -- after the first tool call -- not only when the turn ends.
    # Before a turn (SessionStart / UserPromptSubmit) there is no fresh message AND the user may
    # have just switched via the bar (settings.json already updated, transcript still on the old
    # turn), so there we trust the settings preference -- optimistic, corrected on the next fire.
    assistant = _last_assistant(transcript_path) if event in ("Stop", "PostToolUse") else None
    if assistant is not None:
        model = assistant.get("model") or ""
        tier = (assistant.get("usage") or {}).get("service_tier")
        fast = tier == _FAST_SERVICE_TIER
    else:
        model = managed.get("model") or user_settings.get("model") or ""
        # Managed fastMode wins (command-line precedence); Claude deletes the key on /fast off.
        fast_setting = managed.get("fastMode")
        if not isinstance(fast_setting, bool):
            fast_setting = user_settings.get("fastMode")
        fast = fast_setting is True

    if not isinstance(model, str) or not model:
        return None
    return {"model": model, "effort": effort, "fast": bool(fast)}


def main() -> None:
    env = os.environ
    state_dir = env.get("MNGR_AGENT_STATE_DIR")
    if not state_dir:
        return
    snapshot = compute_snapshot(env, sys.stdin.read())
    if snapshot is None:
        return
    state_path = Path(state_dir)
    tmp = state_path / (_STATE_NAME + ".tmp")
    try:
        tmp.write_text(json.dumps(snapshot))
        tmp.replace(state_path / _STATE_NAME)
    except OSError:
        return


if __name__ == "__main__":
    # Best-effort: the hook command is invoked as ``python3 ... || true`` (see _WRITE_MODEL_STATE
    # in claude_config.py), so a non-zero exit from an unexpected error can never break Claude's
    # hook chain -- no catch-all is needed here, and errors stay visible to tests.
    main()
