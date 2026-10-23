#!/usr/bin/env python3
"""grok's PreToolUse and UserPromptSubmit hook: the workspace's tool-call policies, for grok.

    python3 system/scripts/agent_grok_policy_guards.py pre-tool-use
    python3 system/scripts/agent_grok_policy_guards.py user-prompt-submit

``.grok/hooks/policy.json`` wires both, and each reads grok's hook payload on stdin. grok names
its tools in the payload by their own names (``run_terminal_command``, ``monitor``, ``read_file``,
...), so ``pre-tool-use`` translates each call into the payload the guard scripts beside this file
parse, runs them in ``.claude/settings.json``'s PreToolUse order (``PRETOOLUSE_SCRIPTS``), and
answers in the forms grok honours:

* A refusal exits 2 and carries the guard's reason both on stderr and as the JSON
  ``permissionDecisionReason``: grok shows the model only the first stderr line of a plain exit 2,
  and the JSON reason whole.
* An allowed shell call is rewritten through ``hookSpecificOutput.updatedInput``, which keeps every
  other field of the tool's input and puts the OOM-band and git-identity prefix in front of the
  command. The guards judge the command as the agent wrote it.
* The require-a-step nudge and the open-steps reminder ride ``additionalContext``.

A shell call (``run_terminal_command``, or ``monitor``, whose result is always a background task)
reaches the guards as claude's ``Bash``, with the background flag grok itself applies; any other
tool reaches only the guards that police every tool, under its grok name. A ``use_tool`` wrapper is
judged by the call inside it. ``workflow`` is refused, and so is a shell call whose input grok
truncated, since the truncation hides the end of the command.

grok discards an allowing UserPromptSubmit hook's output, so ``user-prompt-submit`` only records the
turn, and the first PreToolUse of that turn in the same session delivers the open-steps reminder.

Every guard fails open on its own: only its exit 2 refuses, and a crash, a missing script or a run
past the budget is logged to ``$MNGR_AGENT_STATE_DIR/grok_policy_guards.log`` while the call goes
ahead. So does the whole hook, for anything it did not expect.

Standard library only: grok runs its hooks with the system ``python3``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, NamedTuple

SCRIPTS_DIR = Path(__file__).resolve().parent

PRE_TOOL_USE_COMMAND = "pre-tool-use"
USER_PROMPT_SUBMIT_COMMAND = "user-prompt-submit"

LOG_FILE_NAME = "grok_policy_guards.log"
# The turn the open-steps reminder is owed to, as ``{"sessionId", "promptId"}``, and where it moves
# once delivered.
TURN_STAMP_FILE_NAME = "grok_policy_turn.json"
DELIVERED_TURN_STAMP_FILE_NAME = "grok_policy_turn.delivered.json"

# Every script a PreToolUse run starts shares this budget; it stays under the hook's ``timeout`` in
# ``.grok/hooks/policy.json``, so grok never kills the hook before it has answered.
PRE_TOOL_USE_BUDGET_SECONDS = 45.0

SHELL_TOOL_NAMES = frozenset({"run_terminal_command", "monitor"})
ALWAYS_BACKGROUNDED_TOOL_NAME = "monitor"
USE_TOOL_NAME = "use_tool"
WORKFLOW_TOOL_NAME = "workflow"
MAX_USE_TOOL_NESTING = 8
CLAUDE_SHELL_TOOL_NAME = "Bash"

# The variables a guard is started without: a startup file's output would read as the guard's own.
_SHELL_STARTUP_VARIABLES = frozenset({"BASH_ENV", "ENV"})


class Guard(NamedTuple):
    """A blocking guard script, with a loose superset of what it can refuse so a call it could never
    refuse does not start it."""

    script: str
    prefilter: re.Pattern[str]
    # Whether it also polices non-shell tools; its prefilter then reads the whole guard payload.
    is_for_every_tool: bool


BLOCKING_GUARDS: tuple[Guard, ...] = (
    Guard("agent_prevent_commit_rewrite.sh", re.compile(r"\bgit\b"), False),
    Guard("agent_block_pipe_tail_head.sh", re.compile(r"\b(tail|head)\b"), False),
    Guard(
        "agent_latchkey_request_standalone.sh",
        re.compile(r"permission-requests|request_secret\.py"),
        False,
    ),
    Guard("agent_secrets_guard.sh", re.compile(r"\.secrets|\.mcpc"), True),
    # `ticket` does not contain `tk`.
    Guard("agent_tk_standalone.sh", re.compile(r"\b(tk|ticket)\b"), False),
)
STEP_NUDGE_SCRIPT = "agent_require_steps_pretool.sh"
REWRITE_SCRIPT = "agent_rewrite_bash_command.py"
OPEN_STEPS_REMINDER_SCRIPT = "agent_open_tickets_reminder.sh"

PRETOOLUSE_SCRIPTS: tuple[str, ...] = (
    *(guard.script for guard in BLOCKING_GUARDS),
    STEP_NUDGE_SCRIPT,
    REWRITE_SCRIPT,
)

DEFAULT_REFUSAL = "Blocked by a workspace policy check."
WORKFLOW_REFUSAL = (
    "Blocked: the workflow tool is not available in this workspace. Do the work with direct tool "
    "calls instead (spawn_subagent for work that should run in parallel)."
)
TRUNCATED_COMMAND_REFUSAL = (
    "Blocked: this command is too long for the workspace's policy checks to read (grok passes them "
    "only the first 128 KiB of a tool call), so it was not run. Write the long part to a file with "
    "the write tool, then run a short command that uses the file."
)


class DispatchContext(NamedTuple):
    """Where a hook run finds the guard scripts and keeps its state, and what it runs them with."""

    scripts_dir: Path
    # The agent's state dir, or None outside an agent.
    state_dir: Path | None
    environ: Mapping[str, str]
    # The ``time.monotonic()`` value by which every script this run starts must have finished.
    deadline: float


class HookReply(NamedTuple):
    """What the hook writes and the code it exits with."""

    exit_code: int
    stdout: str
    stderr: str


class ToolCall(NamedTuple):
    """The call a PreToolUse payload asks for, with any ``use_tool`` wrapping taken off."""

    tool_name: str
    tool_input: dict[str, object]
    # The ``use_tool`` inputs the call is nested in, outermost first.
    wrappers: tuple[dict[str, object], ...]


ALLOW = HookReply(exit_code=0, stdout="", stderr="")


def log_path_for(state_dir: Path | None) -> Path:
    return (
        state_dir if state_dir is not None else Path(tempfile.gettempdir())
    ) / LOG_FILE_NAME


def note(state_dir: Path | None, message: str) -> None:
    """Record a fail-open path, so a guard that stopped running is visible. Never raises."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with log_path_for(state_dir).open("a") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def session_id_of(payload: Mapping[str, object]) -> str | None:
    value = payload.get("sessionId", payload.get("session_id"))
    return value if isinstance(value, str) and value else None


def is_use_tool_wrapper(tool_name: str, tool_input: Mapping[str, object]) -> bool:
    """Whether a call's input is ``use_tool``'s inline form, which grok reports under the inner
    tool's name: ``{"tool_name": <inner>, "tool_input": {...}}``."""
    inner_name = tool_input.get("tool_name")
    if not isinstance(inner_name, str) or not isinstance(
        tool_input.get("tool_input"), dict
    ):
        return False
    return tool_name in (USE_TOOL_NAME, inner_name)


def read_tool_call(payload: Mapping[str, object]) -> ToolCall | None:
    tool_name = payload.get("tool_name", payload.get("toolName"))
    tool_input = payload.get("tool_input", payload.get("toolInput"))
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return None
    wrappers: list[dict[str, object]] = []
    while len(wrappers) < MAX_USE_TOOL_NESTING and is_use_tool_wrapper(
        tool_name, tool_input
    ):
        wrappers.append(tool_input)
        tool_name = str(tool_input["tool_name"])
        tool_input = dict(tool_input["tool_input"])
    return ToolCall(
        tool_name=tool_name, tool_input=tool_input, wrappers=tuple(wrappers)
    )


def rewrap(
    wrappers: Sequence[dict[str, object]], inner_input: dict[str, object]
) -> dict[str, object]:
    """``inner_input`` put back inside the ``use_tool`` inputs it came out of."""
    result = inner_input
    for wrapper in reversed(wrappers):
        result = {**wrapper, "tool_input": result}
    return result


def _is_zero(value: object) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0
    )


def is_backgrounded(tool_name: str, tool_input: Mapping[str, object]) -> bool:
    """Whether grok runs a shell call in the background: a ``monitor`` always; otherwise, when
    ``block_until_ms`` is set, exactly when it is 0, and when it is not, when ``is_background`` is
    true or ``timeout`` is 0."""
    if tool_name == ALWAYS_BACKGROUNDED_TOOL_NAME:
        return True
    block_until_ms = tool_input.get("block_until_ms")
    if block_until_ms is not None:
        return _is_zero(block_until_ms)
    return tool_input.get("is_background") is True or _is_zero(
        tool_input.get("timeout")
    )


def shell_guard_payload(command: str, is_background: bool) -> dict[str, object]:
    return {
        "tool_name": CLAUDE_SHELL_TOOL_NAME,
        "tool_input": {"command": command, "run_in_background": is_background},
    }


def run_script(
    script: str, stdin_text: str, context: DispatchContext
) -> subprocess.CompletedProcess[str] | None:
    """Run one script beside this file under bash, or return None (and log why) when it could not
    run to completion within the budget."""
    path = context.scripts_dir / script
    if not path.is_file():
        note(context.state_dir, f"{script} is missing; skipping it")
        return None
    remaining_seconds = context.deadline - time.monotonic()
    if remaining_seconds <= 0:
        note(context.state_dir, f"{script} skipped: the hook's budget is spent")
        return None
    environ = {
        key: value
        for key, value in context.environ.items()
        if key not in _SHELL_STARTUP_VARIABLES
    }
    try:
        # A session of its own, so a run past the budget is stopped with everything it started.
        process = subprocess.Popen(
            ["/bin/bash", str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environ,
            start_new_session=True,
        )
    except OSError as error:
        note(context.state_dir, f"{script} could not start: {error}; failing open")
        return None
    try:
        stdout, stderr = process.communicate(stdin_text, timeout=remaining_seconds)
    except subprocess.TimeoutExpired:
        stop_process_group(process)
        note(context.state_dir, f"{script} ran past the hook's budget; failing open")
        return None
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def stop_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


def first_refusal(
    guard_payload: Mapping[str, object], command: str | None, context: DispatchContext
) -> str | None:
    """The reason the first refusing guard gives, or None when every guard lets the call through.

    ``command`` is the shell command for a shell call and None for any other tool, which only the
    guards for every tool see.
    """
    payload_json = json.dumps(guard_payload)
    for guard in BLOCKING_GUARDS:
        if command is None and not guard.is_for_every_tool:
            continue
        searched_text = (
            payload_json if guard.is_for_every_tool or command is None else command
        )
        if not guard.prefilter.search(searched_text):
            continue
        result = run_script(guard.script, payload_json, context)
        if result is None:
            continue
        if result.returncode == 2:
            return result.stderr.strip() or DEFAULT_REFUSAL
        if result.returncode != 0:
            note(
                context.state_dir,
                f"{guard.script} exited {result.returncode}: {_first_line(result.stderr)}; failing open",
            )
    return None


def step_nudge(
    guard_payload: Mapping[str, object], context: DispatchContext
) -> str | None:
    """The require-a-step reminder for this call, or None."""
    result = run_script(STEP_NUDGE_SCRIPT, json.dumps(guard_payload), context)
    if result is None:
        return None
    if result.returncode != 0:
        note(
            context.state_dir,
            f"{STEP_NUDGE_SCRIPT} exited {result.returncode}: {_first_line(result.stderr)}; no nudge",
        )
        return None
    if not result.stdout.strip():
        return None
    try:
        reply = json.loads(result.stdout)
    except ValueError:
        note(
            context.state_dir,
            f"{STEP_NUDGE_SCRIPT} printed something that is not JSON; no nudge",
        )
        return None
    hook_output = reply.get("hookSpecificOutput") if isinstance(reply, dict) else None
    text = (
        hook_output.get("additionalContext") if isinstance(hook_output, dict) else None
    )
    return text.strip() if isinstance(text, str) and text.strip() else None


def claim_open_steps_reminder(
    session_id: str | None, context: DispatchContext
) -> str | None:
    """The open-steps reminder when this is the first tool call of a turn of ``session_id`` that
    ``user-prompt-submit`` recorded, else None. Claiming the turn moves its stamp aside, so the
    reminder is delivered once even when several calls race for it."""
    if context.state_dir is None or session_id is None:
        return None
    pending = context.state_dir / TURN_STAMP_FILE_NAME
    try:
        stamp = json.loads(pending.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        note(context.state_dir, f"could not read {TURN_STAMP_FILE_NAME}: {error}")
        return None
    if not isinstance(stamp, dict) or stamp.get("sessionId") != session_id:
        return None
    try:
        os.replace(pending, context.state_dir / DELIVERED_TURN_STAMP_FILE_NAME)
    except FileNotFoundError:
        return None
    result = run_script(OPEN_STEPS_REMINDER_SCRIPT, "", context)
    if result is None:
        return None
    if result.returncode != 0:
        note(
            context.state_dir,
            f"{OPEN_STEPS_REMINDER_SCRIPT} exited {result.returncode}: {_first_line(result.stderr)}",
        )
        return None
    return result.stdout.strip() or None


def rewrite_prefix(context: DispatchContext) -> str | None:
    """The OOM-band and git-identity prefix for a shell command, or None (logged) when it cannot be
    built, in which case the command runs as written."""
    path = context.scripts_dir / REWRITE_SCRIPT
    # Loaded here rather than imported at the top, so a broken rewriter costs only the prefix.
    try:
        spec = importlib.util.spec_from_file_location(
            "agent_rewrite_bash_command", path
        )
        if spec is None or spec.loader is None:
            note(
                context.state_dir,
                f"{REWRITE_SCRIPT} cannot be loaded; running the command unprefixed",
            )
            return None
        rewriter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rewriter)
        return rewriter.build_rewritten_command("")
    except Exception as error:
        note(
            context.state_dir,
            f"{REWRITE_SCRIPT} failed: {error!r}; running the command unprefixed",
        )
        return None


def deny_reply(reason: str) -> HookReply:
    decision = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    return HookReply(
        exit_code=2,
        stdout=json.dumps({"hookSpecificOutput": decision}),
        stderr=reason + "\n",
    )


def allow_reply(
    updated_input: dict[str, object] | None, notes_for_the_model: Sequence[str]
) -> HookReply:
    hook_output: dict[str, object] = {"hookEventName": "PreToolUse"}
    if updated_input is not None:
        hook_output["updatedInput"] = updated_input
    if notes_for_the_model:
        hook_output["additionalContext"] = "\n\n".join(notes_for_the_model)
    if len(hook_output) == 1:
        return ALLOW
    return HookReply(
        exit_code=0, stdout=json.dumps({"hookSpecificOutput": hook_output}), stderr=""
    )


def notes_for_allowed_call(
    payload: Mapping[str, object],
    guard_payload: Mapping[str, object],
    context: DispatchContext,
) -> list[str]:
    notes = [
        claim_open_steps_reminder(session_id_of(payload), context),
        step_nudge(guard_payload, context),
    ]
    return [text for text in notes if text is not None]


def handle_pre_tool_use(
    payload: Mapping[str, object], context: DispatchContext
) -> HookReply:
    call = read_tool_call(payload)
    if call is None:
        return ALLOW
    if call.tool_name == WORKFLOW_TOOL_NAME:
        return deny_reply(WORKFLOW_REFUSAL)
    if call.tool_name not in SHELL_TOOL_NAMES:
        guard_payload = {"tool_name": call.tool_name, "tool_input": call.tool_input}
        refusal = first_refusal(guard_payload, None, context)
        if refusal is not None:
            return deny_reply(refusal)
        return allow_reply(
            None, notes_for_allowed_call(payload, guard_payload, context)
        )

    command = call.tool_input.get("command")
    if not isinstance(command, str) or not command:
        return ALLOW
    if payload.get("toolInputTruncated") is True:
        return deny_reply(TRUNCATED_COMMAND_REFUSAL)
    guard_payload = shell_guard_payload(
        command, is_backgrounded(call.tool_name, call.tool_input)
    )
    refusal = first_refusal(guard_payload, command, context)
    if refusal is not None:
        return deny_reply(refusal)
    notes = notes_for_allowed_call(payload, guard_payload, context)
    prefix = rewrite_prefix(context)
    updated_input = (
        None
        if prefix is None
        else rewrap(call.wrappers, {**call.tool_input, "command": prefix + command})
    )
    return allow_reply(updated_input, notes)


def handle_user_prompt_submit(
    payload: Mapping[str, object], context: DispatchContext
) -> HookReply:
    """Record the turn a root session's prompt opens. A subagent's turn is not the session's."""
    if payload.get("subagentType"):
        return ALLOW
    session_id = session_id_of(payload)
    if session_id is None:
        return ALLOW
    if context.state_dir is None:
        note(None, "no MNGR_AGENT_STATE_DIR; the open-steps reminder is off")
        return ALLOW
    stamp = json.dumps({"sessionId": session_id, "promptId": payload.get("promptId")})
    pending = context.state_dir / TURN_STAMP_FILE_NAME
    staging = pending.with_name(f"{pending.name}.{os.getpid()}.tmp")
    try:
        staging.write_text(stamp)
        os.replace(staging, pending)
    except OSError as error:
        note(context.state_dir, f"could not record the turn: {error}")
    return ALLOW


def read_payload(stdin: IO[str], state_dir: Path | None) -> Mapping[str, object] | None:
    raw = stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        note(
            state_dir,
            f"the hook input is not JSON ({len(raw)} bytes); allowing the call",
        )
        return None
    return payload if isinstance(payload, dict) else None


def run_hook(
    argv: Sequence[str], stdin: IO[str], context: DispatchContext
) -> HookReply:
    command = argv[0] if argv else ""
    if command not in (PRE_TOOL_USE_COMMAND, USER_PROMPT_SUBMIT_COMMAND):
        note(context.state_dir, f"unknown hook command {command!r}; allowing the call")
        return ALLOW
    payload = read_payload(stdin, context.state_dir)
    if payload is None:
        return ALLOW
    if command == PRE_TOOL_USE_COMMAND:
        return handle_pre_tool_use(payload, context)
    return handle_user_prompt_submit(payload, context)


def context_for(environ: Mapping[str, str]) -> DispatchContext:
    state_dir = environ.get("MNGR_AGENT_STATE_DIR")
    return DispatchContext(
        scripts_dir=SCRIPTS_DIR,
        state_dir=Path(state_dir) if state_dir else None,
        environ=environ,
        deadline=time.monotonic() + PRE_TOOL_USE_BUDGET_SECONDS,
    )


def main(
    argv: Sequence[str],
    stdin: IO[str],
    stdout: IO[str],
    stderr: IO[str],
    environ: Mapping[str, str],
) -> int:
    context = context_for(environ)
    # The hook sits in front of every tool call: whatever goes wrong, the call goes ahead.
    try:
        reply = run_hook(argv, stdin, context)
    except Exception as error:
        note(
            context.state_dir, f"{' '.join(argv)} failed: {error!r}; allowing the call"
        )
        return 0
    stdout.write(reply.stdout)
    stderr.write(reply.stderr)
    return reply.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin, sys.stdout, sys.stderr, os.environ))
