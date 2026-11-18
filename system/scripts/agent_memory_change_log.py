#!/usr/bin/env python3
"""Claude's hooks that record every change the agent makes to its memory folder.

Wired in ``.claude/settings.json``:

* ``sync`` (UserPromptSubmit) -- start the turn: save the folder as this session's baseline.
* ``post`` (PostToolUse) -- fold what the tool call changed into the turn's change records
  (one per note, the index split by line), and tell the agent each record it changed.

The logic lives in ``agent_memory.changes`` (system/libs/agent_memory), which the chat app
also imports to show each change as a card in the conversation with an Undo button. Memory is
Claude's alone (codex's is switched off, pi and agy are told not to use one), so no other
harness wires these. They are not PreToolUse guards (tool-call-policies.md): they only record.

Never fails the tool call: any error goes to stderr and the hook exits 0. Run it as
``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set, since it puts the library's
sources on sys.path.
"""

import json
import os
import sys
from pathlib import Path

# import-path: no-venv -- beyond the standard library it imports only the stdlib-only `agent_memory.changes`
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "agent_memory" / "src")
)

from agent_memory.changes import default_log  # noqa: E402
from agent_memory.changes import format_agent_note  # noqa: E402


def run(mode: str, payload: dict) -> str | None:
    log = default_log()
    session_id = str(payload.get("session_id") or "unknown")
    if mode == "sync":
        log.start_turn(session_id)
        return None
    if mode == "post":
        changes = log.record_tool_call(
            session_id,
            str(payload.get("tool_use_id") or ""),
            payload.get("tool_input", {}),
            chat_id=os.environ.get("MINDS_CHAT_ID")
            or os.environ.get("MNGR_AGENT_ID")
            or "",
            agent_name=os.environ.get("MNGR_AGENT_NAME", ""),
        )
        # One line per record the call touched (one per note, one for the index's other lines).
        return "\n".join(format_agent_note(change) for change in changes) or None
    return None


def main(argv: list[str]) -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        note = run(argv[1] if len(argv) > 1 else "", payload)
        if note:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PostToolUse",
                            "additionalContext": note,
                        }
                    }
                )
            )
    except Exception as error:  # never break the agent's tool call
        print(f"memory change log: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
