#!/usr/bin/env python3
"""Claude hooks for the steps the chat app makes for the user's messages.

When a user sends a chat a message, the chat app reserves a tk step id for it before typing it to the agent, then
asks a small model what the message does to the chat's open steps (usually a few seconds; longer when the call is
retried) (``system/apps/chat/imbue/chat/message_steps.py``). A new request gets its step written under the reserved
id, an ordinary tk step record in ``$TICKETS_DIR`` created with the chat's id as its creator. So does a message that
changes what an open step asks for, and the chat cancels the step it replaces with ``tk cancel`` (a sidecar tk honours
until the agent starts the step again). A message that repeats an open step gets no step of its own, and neither does
one that calls an open step off, which the chat cancels the same way. The model sees one message at a time, so the
agent is told its decisions as suggestions to check against everything the user said. A reservation is one JSON file under
``$CHAT_MESSAGE_STEP_RESERVATIONS_DIR``; every id that ends without a step of its own has its outcome recorded under
``settled/`` beside it, naming the open step when there is one.

Two hooks, one subcommand each:

- ``prompt`` (UserPromptSubmit): find the reservation (or the step) made for this message by its text and tell
  the agent its id, so the agent works under it and never makes its own step for the request. Claude runs the
  hook when the message is submitted, also while the agent is busy and the message waits in its queue; the
  hook's text reaches the agent together with the message.
- ``wait`` (PreToolUse): a ``tk start`` or ``tk close`` of a reserved id whose step is not written yet waits for
  it, at most as long as the chat app tries. An id judged not a task, or whose message belongs to an open step, is
  answered at once: the command is declined with that reason, which is not a failure. An id whose step could not be
  created blocks the command with an error. Either way the command never reaches tk, which would only say the id is
  unknown. Records of another chat are ignored, as if the id were never reserved here.

Standard library only: Claude runs hooks under a plain ``python3``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, NamedTuple

# Kept equal to the chat app's MESSAGE_STEP_TAG, MESSAGE_REF_PREFIX and MESSAGE_STEP_RESERVATIONS_DIRNAME
# (message_steps.py).
MESSAGE_STEP_TAG = "user-message"
MESSAGE_REF_PREFIX = "message:"
RESERVATIONS_DIR_ENV = "CHAT_MESSAGE_STEP_RESERVATIONS_DIR"
DEFAULT_RESERVATIONS_DIR = Path("data") / ".apps" / "chat" / "message_step_reservations"
# The longest the chat app takes to write a step: TITLE_CALL_TIMEOUT_SECONDS times (TITLE_CALL_RETRY_COUNT + 1)
# in message_steps.py, plus a margin for writing the file.
MAX_WAIT_SECONDS = 10.0 * 3 + 5.0
# Kept equal to the chat app's SETTLED_DIRNAME and MessageStepOutcome values (message_steps.py).
SETTLED_DIRNAME = "settled"
OUTCOME_NOT_A_TASK = "not_a_task"
OUTCOME_FAILED = "failed"
OUTCOME_SAME_AS = "same_as"
OUTCOME_CANCELS = "cancels"
_SETTLED_OUTCOMES = (
    OUTCOME_NOT_A_TASK,
    OUTCOME_FAILED,
    OUTCOME_SAME_AS,
    OUTCOME_CANCELS,
)
OUTCOME_WRITTEN = "written"
OUTCOME_UNKNOWN = "unknown"
_POLL_SECONDS = 0.2
_FRONT_MATTER_DELIMITER = "---"
# A tk start or close of a step id, as its own command within a shell line.
_TK_STEP_COMMAND = re.compile(
    r"(?:^|[;&|(\s])(?:tk|ticket)\s+(?:start|close)\s+(\S+-step-[a-z0-9]+)"
)


class PendingStep(NamedTuple):
    """A message's step as this hook sees it, reserved (not written yet) or written."""

    step_id: str
    creator: str
    message_text: str
    # When the id was reserved or the step written: the newest of a repeated message's steps is the latest send's.
    since: str


def normalized_message_text(text: str) -> str:
    """A message's text as two copies of it compare: line endings unified, outer whitespace dropped."""
    return text.replace("\r\n", "\n").strip()


def parse_message_step_record(text: str) -> PendingStep | None:
    """The message step a tk record describes, or None for any record that is not one."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONT_MATTER_DELIMITER:
        return None
    fields: dict[str, str] = {}
    body_start = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONT_MATTER_DELIMITER:
            body_start = index + 1
            break
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    if body_start is None:
        return None
    tags = tuple(tag.strip() for tag in fields.get("tags", "").strip("[]").split(","))
    if (
        fields.get("step") != "true"
        or MESSAGE_STEP_TAG not in tags
        or not fields.get("external-ref", "").startswith(MESSAGE_REF_PREFIX)
    ):
        return None
    body = lines[body_start:]
    title_index = next(
        (index for index, line in enumerate(body) if line.startswith("# ")), None
    )
    if title_index is None:
        return None
    after_title = body[title_index + 1 :]
    # tk appends the close summary as the record's last section, whatever the message itself contains.
    summary_index = max(
        (
            index
            for index, line in enumerate(after_title)
            if line.strip() == "## Summary"
        ),
        default=None,
    )
    return PendingStep(
        step_id=fields.get("id", ""),
        creator=fields.get("agent", ""),
        message_text="\n".join(after_title[:summary_index]).strip(),
        since=fields.get("created", ""),
    )


def parse_reservation(text: str) -> PendingStep | None:
    try:
        record = json.loads(text)
    except ValueError:
        return None
    if not isinstance(record, dict) or not isinstance(record.get("step_id"), str):
        return None
    return PendingStep(
        step_id=record["step_id"],
        creator=str(record.get("chat_id", "")),
        message_text=str(record.get("message_text", "")),
        since=str(record.get("reserved_at", "")),
    )


def _read_parsed(
    paths: Sequence[Path], parse: Callable[[str], PendingStep | None]
) -> list[PendingStep]:
    parsed: list[PendingStep] = []
    for path in paths:
        try:
            step = parse(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if step is not None:
            parsed.append(step)
    return parsed


def read_pending_steps(tickets_dir: Path, reservations_dir: Path) -> list[PendingStep]:
    """Every message step, written or only reserved."""
    written = _read_parsed(
        sorted(tickets_dir.glob("*-step-*.md")), parse_message_step_record
    )
    reserved = _read_parsed(sorted(reservations_dir.glob("*.json")), parse_reservation)
    return [*written, *reserved]


def step_for_prompt(
    steps: Sequence[PendingStep], prompt: str, creator: str
) -> PendingStep | None:
    """The step made for this message: the newest with its text among ``creator``'s (every chat's when unset).

    The step is normally still a reservation here, made a moment before the message was typed; a written one counts
    too, for a message whose typing was held up past its title call.
    """
    wanted = normalized_message_text(prompt)
    matching = [
        step
        for step in steps
        if wanted
        and normalized_message_text(step.message_text) == wanted
        and (not creator or step.creator == creator)
    ]
    return max(matching, key=lambda step: step.since, default=None)


def message_step_context(step: PendingStep) -> str:
    return (
        "[Message step]\n"
        f"If this message is a new task, its step is being created under id {step.step_id}: it appears on the user's "
        "progress timeline within seconds, titled with their request. These decisions come from the chat's quick "
        "model, which sees one message at a time: treat them as suggestions, and check them against everything the "
        "user has said. If the model judges that the message repeats or calls off one of your open steps, `tk start` "
        "on this id tells you which; if it changes one, its step replaces that one, and the chat cancels the old step "
        "(`tk cancel`; starting the old step again overrides that). If the model judges it not a task (thanks, a "
        "question about progress), no step is created. Either way, do not create a second step for this message; you "
        "may add steps for parts of your own work.\n"
        f"- When you start on this message, run `tk start {step.step_id}` (it waits until the step exists) and do "
        "its work under that step.\n"
        f'- When it is done, run `tk close {step.step_id} "<one-line summary of the work>"`.\n'
        f"- If `tk start {step.step_id}` says the model judged the message not a task, reply or carry on without a step, "
        "or create one if you judge it is a task.\n"
        f"- If `tk start {step.step_id}` says the message repeats or calls off another step, decide what to do with that "
        "step as it suggests."
    )


def _tickets_and_reservations_dirs(environ: Mapping[str, str]) -> tuple[Path, Path]:
    work_dir = Path(environ.get("MNGR_AGENT_WORK_DIR") or os.getcwd())
    tickets_dir = Path(environ.get("TICKETS_DIR") or work_dir / ".tickets")
    reservations_dir = Path(
        environ.get(RESERVATIONS_DIR_ENV) or work_dir / DEFAULT_RESERVATIONS_DIR
    )
    return tickets_dir, reservations_dir


def cmd_prompt(payload: Mapping[str, object], environ: Mapping[str, str]) -> int:
    prompt = payload.get("prompt")
    # A subagent's prompt is its parent's request, never a message from the user.
    if not isinstance(prompt, str) or environ.get("MNGR_CLAUDE_SUBAGENT_PROXY_CHILD"):
        return 0
    tickets_dir, reservations_dir = _tickets_and_reservations_dirs(environ)
    # The creator tk stamps on a step (its tk_creator): the chat's id, else the agent's name.
    creator = environ.get("MINDS_CHAT_ID") or environ.get("MNGR_AGENT_NAME") or ""
    step = step_for_prompt(
        read_pending_steps(tickets_dir, reservations_dir), prompt, creator
    )
    if step is None:
        return 0
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": message_step_context(step),
        }
    }
    print(json.dumps(output))
    return 0


def reserved_step_ids_in_command(command: str) -> list[str]:
    return _TK_STEP_COMMAND.findall(command)


class Settled(NamedTuple):
    """How a reserved id ended without a step of its own, from its record under ``settled/``."""

    outcome: str
    # The open step the message repeats or calls off, for same_as and cancels; '' otherwise.
    target_id: str
    # That step's title.
    target_title: str
    # Why the step is no longer needed, for cancels; '' otherwise.
    reason: str
    # The chat the message was sent to.
    chat_id: str


def read_settled(reservations_dir: Path, step_id: str) -> Settled | None:
    try:
        record = json.loads(
            (reservations_dir / SETTLED_DIRNAME / f"{step_id}.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("outcome") not in _SETTLED_OUTCOMES:
        return None
    return Settled(
        outcome=str(record["outcome"]),
        target_id=str(record.get("target_id", "")),
        target_title=str(record.get("target_title", "")),
        reason=str(record.get("reason", "")),
        chat_id=str(record.get("chat_id", "")),
    )


def _settled_outcome(reservations_dir: Path, step_id: str) -> str | None:
    settled = read_settled(reservations_dir, step_id)
    return None if settled is None else settled.outcome


def _is_open(fields: Mapping[str, str]) -> bool:
    return fields.get("status") in ("open", "in_progress")


def _is_open(fields: Mapping[str, str]) -> bool:
    return fields.get("status") in ("open", "in_progress")


def belonging_note(settled: Settled, target_fields: Mapping[str, str]) -> str | None:
    """What to tell the agent about a message the chat's model judged to repeat or call off an open step, given that
    step's record now, or None for any other outcome."""
    target = f'step {settled.target_id} "{settled.target_title}"'
    judged = "The chat's quick model, which sees only this message, judged"
    if settled.outcome == OUTCOME_SAME_AS and not _is_open(target_fields):
        return (
            f"{judged} that it asks for what {target} covered, and that step is already done. If that is right after "
            "everything the user has said, reply or carry on without a step; if not, create a step for it."
        )
    if settled.outcome == OUTCOME_SAME_AS:
        return (
            f"{judged} that it asks for what {target} already covers, so no step was made for it. If that is right "
            f"after everything the user has said, do it under {settled.target_id}; if not, create a step for it."
        )
    if settled.outcome == OUTCOME_CANCELS and not _is_open(target_fields):
        return (
            f"{judged} that it calls off {target}, which was already done before it arrived. Tell the user it was "
            "already done."
        )
    if settled.outcome == OUTCOME_CANCELS:
        return (
            f"{judged} that it calls off {target} ({settled.reason}), so no step was made for it, and the chat "
            "recorded that with `tk cancel`: the step no longer shows to the user. If that is right after everything "
            f'the user has said, stop that work and close the step: `tk close {settled.target_id} "No longer needed: '
            f'{settled.reason}"`. If not, run `tk start {settled.target_id}` to keep it, which overrides the cancel.'
        )
    return None


def read_step_fields(tickets_dir: Path, step_id: str) -> dict[str, str]:
    """A step record's front matter fields and its title (under ``title``), or {} when it cannot be read."""
    try:
        lines = (tickets_dir / f"{step_id}.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    if not lines or lines[0].strip() != _FRONT_MATTER_DELIMITER:
        return {}
    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONT_MATTER_DELIMITER:
            fields["title"] = next(
                (
                    rest[2:].strip()
                    for rest in lines[index + 1 :]
                    if rest.startswith("# ")
                ),
                "",
            )
            return fields
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    return {}


def wait_for_step(
    step_id: str,
    tickets_dir: Path,
    reservations_dir: Path,
    max_wait_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """How a reserved id ended, waiting while it is reserved and unwritten: written, not a task, failed, or unknown
    (never reserved here, or still unwritten when the wait ran out)."""
    step_path = tickets_dir / f"{step_id}.md"
    reservation_path = reservations_dir / f"{step_id}.json"
    deadline = monotonic() + max_wait_seconds
    outcome = _current_outcome(step_path, reservation_path, reservations_dir, step_id)
    while outcome is None and monotonic() < deadline:
        sleep(_POLL_SECONDS)
        outcome = _current_outcome(
            step_path, reservation_path, reservations_dir, step_id
        )
    return outcome if outcome is not None else OUTCOME_UNKNOWN


def _current_outcome(
    step_path: Path, reservation_path: Path, reservations_dir: Path, step_id: str
) -> str | None:
    """How the reserved id stands now, or None while it is reserved and unwritten."""
    if step_path.exists():
        return OUTCOME_WRITTEN
    settled = _settled_outcome(reservations_dir, step_id)
    if settled is not None:
        return settled
    if reservation_path.exists():
        return None
    # Written and released between the checks reads as written.
    return OUTCOME_WRITTEN if step_path.exists() else OUTCOME_UNKNOWN


def is_other_chats(reservations_dir: Path, step_id: str, chat_id: str) -> bool:
    """Whether a reserved id's records name another chat than the agent's (records without a chat count as its own)."""
    if not chat_id:
        return False
    try:
        pending = parse_reservation(
            (reservations_dir / f"{step_id}.json").read_text(encoding="utf-8")
        )
    except OSError:
        pending = None
    settled = read_settled(reservations_dir, step_id)
    owners = {pending.creator if pending else "", settled.chat_id if settled else ""}
    return any(owner and owner != chat_id for owner in owners)


def _deny(reason: str) -> int:
    decision = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    print(json.dumps({"hookSpecificOutput": decision}))
    return 0


def cmd_wait(
    payload: Mapping[str, object], environ: Mapping[str, str], max_wait_seconds: float
) -> int:
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if payload.get("tool_name") not in (None, "", "Bash") or not isinstance(
        command, str
    ):
        return 0
    tickets_dir, reservations_dir = _tickets_and_reservations_dirs(environ)
    separately = " Run any other tk commands from this call separately."
    chat_id = environ.get("MINDS_CHAT_ID", "")
    for step_id in reserved_step_ids_in_command(command):
        # Another chat's message is none of this agent's: tk answers as for any id.
        if is_other_chats(reservations_dir, step_id, chat_id):
            continue
        outcome = wait_for_step(
            step_id, tickets_dir, reservations_dir, max_wait_seconds
        )
        if outcome == OUTCOME_NOT_A_TASK:
            return _deny(
                f"No step {step_id}: the chat's quick model, which sees only this message, judged it not to be a task, "
                "so no step was made for it. If that is right after everything the user has said, reply or carry on "
                "without one; if not, create a step for it." + separately
            )
        if outcome in (OUTCOME_SAME_AS, OUTCOME_CANCELS):
            settled = read_settled(reservations_dir, step_id)
            note = (
                None
                if settled is None
                else belonging_note(
                    settled, read_step_fields(tickets_dir, settled.target_id)
                )
            )
            if note is not None:
                return _deny(f"No step {step_id}. {note}" + separately)
        if outcome == OUTCOME_FAILED:
            sys.stderr.write(
                f"No step {step_id}: the step for this message could not be created. Carry on with the work without "
                "it, and do not create a replacement step.\n"
            )
            return 2
    return 0


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    stdin: IO[str] | None = None,
    max_wait_seconds: float = MAX_WAIT_SECONDS,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prompt", "wait"))
    args = parser.parse_args(argv)
    resolved_environ = os.environ if environ is None else environ
    resolved_stdin = sys.stdin if stdin is None else stdin
    try:
        payload = json.loads(resolved_stdin.read())
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    if args.command == "prompt":
        return cmd_prompt(payload, resolved_environ)
    return cmd_wait(payload, resolved_environ, max_wait_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
