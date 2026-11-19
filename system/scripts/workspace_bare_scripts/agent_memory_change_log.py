"""Claude's hooks that record every change the agent makes to its memory folder.

    python3 system/scripts/agent_memory_change_log.py sync
    python3 system/scripts/agent_memory_change_log.py post

``.claude/settings.json`` wires both, and each reads the hook's input from stdin:

* ``sync`` (UserPromptSubmit) -- start the turn: save the folder as this session's baseline.
* ``post`` (PostToolUse) -- fold what the tool call changed into the turn's change records
  (one per note, the index split by line), and tell the agent each record it changed.

The logic lives in ``agent_memory.changes`` (system/libs/agent_memory), which the chat app
also imports to show each change as a card in the conversation with an Undo button; the entry
script ``system/scripts/agent_memory_change_log.py`` puts it (and the ``chat_background_tasks``
it reads) on ``sys.path``, since the hooks run a bare ``python3``. Memory is Claude's alone
(codex's is switched off, pi and agy are told not to use one), so no other harness wires
these. They are not PreToolUse guards (tool-call-policies.md): they only record.

Each never fails the tool call: an unreadable input or a folder it cannot read or write goes to
stderr, and the hook exits 0.
"""

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import IO

from agent_memory.changes import default_log, format_agent_note
from chat_background_tasks.markers import json_object, own_chat_id

_SCRIPT_NAME = "agent_memory_change_log.py"
_UNKNOWN_SESSION_ID = "unknown"


def _read_payload(stdin: IO[str], event: str) -> dict[str, object] | None:
    """The hook's input object, or None (said on stderr) when it is not a JSON object."""
    try:
        payload = json.loads(stdin.read() or "null")
    except ValueError as error:
        print(
            f"{_SCRIPT_NAME}: the {event} input is not JSON ({error})", file=sys.stderr
        )
        return None
    return json_object(payload)


def _session_id(payload: Mapping[str, object]) -> str:
    return str(payload.get("session_id") or _UNKNOWN_SESSION_ID)


def cmd_sync(stdin: IO[str], environ: Mapping[str, str]) -> int:
    payload = _read_payload(stdin, "UserPromptSubmit")
    if payload is None:
        return 0
    try:
        default_log(environ).start_turn(_session_id(payload))
    except (OSError, ValueError) as error:
        print(f"{_SCRIPT_NAME}: could not start the turn: {error}", file=sys.stderr)
    return 0


def post_tool_use_output(note: str) -> str:
    """The PostToolUse hook output that hands ``note`` to the agent as additional context."""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": note,
            }
        }
    )


def cmd_post(stdin: IO[str], stdout: IO[str], environ: Mapping[str, str]) -> int:
    payload = _read_payload(stdin, "PostToolUse")
    if payload is None:
        return 0
    try:
        changes = default_log(environ).record_tool_call(
            _session_id(payload),
            str(payload.get("tool_use_id") or ""),
            payload.get("tool_input", {}),
            chat_id=own_chat_id(environ),
            agent_name=environ.get("MNGR_AGENT_NAME", ""),
        )
    except (OSError, ValueError) as error:
        print(
            f"{_SCRIPT_NAME}: could not record the tool call: {error}", file=sys.stderr
        )
        return 0
    # One line per record the call touched (one per note, one for the index's other lines).
    note = "\n".join(format_agent_note(change.id, change.summary) for change in changes)
    if note:
        print(post_tool_use_output(note), file=stdout)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Claude's hooks that record every change the agent makes to its memory."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "sync",
        help="The UserPromptSubmit hook: save the folder as the turn's baseline.",
    )
    commands.add_parser(
        "post",
        help="The PostToolUse hook: record what the tool call changed and tell the agent.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    resolved_environ = os.environ if environ is None else environ
    resolved_stdin = sys.stdin if stdin is None else stdin
    if args.command == "sync":
        return cmd_sync(resolved_stdin, resolved_environ)
    return cmd_post(
        resolved_stdin, sys.stdout if stdout is None else stdout, resolved_environ
    )
