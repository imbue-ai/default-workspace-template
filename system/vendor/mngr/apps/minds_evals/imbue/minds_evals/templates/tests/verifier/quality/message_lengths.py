"""The message-length guard: the fraction of the agent's turns whose messages stay inside the limits
a client-facing turn is expected to keep. It is one of the equal-weight `quality` criteria,
alongside the judged ones, and is not a hard gate. Runs in the verifier container: stdlib +
rewardkit only, absolute paths.

A turn has a shape, not just a size. The messages before the last one are status updates the client
reads in passing while the agent works, so they are held to a short line; the last message is the
turn's answer -- what was delivered, how to use it, what the client has to decide -- and is allowed
real length. Scoring each message against the limit for its position, rather than averaging the turn,
is what keeps the measurement about how the agent writes instead of about how the work happened to
divide across turns: an agent that gates on a mock-up delivers in its second turn and an agent that
does not delivers in its first, and both are keeping the same discipline.

The score is the fraction of turns that keep it, so one overlong turn in three costs a third rather
than the whole criterion.

Which message ends a turn is recorded, not inferred: the workspace's own document marks it with a
terminal `finish_reason`, where every interim message carries `tool_use`. That distinction is
load-bearing for a turn the trial cut short, whose last recorded message is a status line rather than
an answer -- position would grade it as the turn's answer and let 300 words through. Position is the
fallback for a turn that marks nothing, which is the driver's hand-built trajectory: there a turn is
a single merged step, so it has no interim messages and only the final-message limit applies.

The limits below are the whole configuration: the bar is a property of the writing rather than of the
eval config, and it is per message so that a wall of text cannot be paid for by the terse messages
around it. The driver's own `average_words_per_turn` and `average_words_per_message` are recorded in
the trial metadata for observability only; nothing at grade time reads either.
"""

import json
from pathlib import Path
from typing import Any

from rewardkit import criterion

TRAJECTORY_PATH = Path("/logs/agent/trajectory.json")

# The turn's answer: the delivery message, the handoff, the question the client has to settle.
FINAL_MESSAGE_WORD_LIMIT = 300
# Everything before it: a status line the client reads while the agent is still working.
INTERIM_MESSAGE_WORD_LIMIT = 30


def _trajectory_steps(trajectory_path: Path) -> list[dict[str, Any]] | None:
    try:
        document = json.loads(trajectory_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    steps = document.get("steps")
    if not isinstance(steps, list):
        return None
    return [step for step in steps if isinstance(step, dict)]


# The stop reasons that mean "this was the last message of the turn". `end_turn` is the ordinary one;
# a turn can also end because a stop sequence fired or the model hit its output limit. Treating only
# `end_turn` as terminal reads a delivery message truncated at `max_tokens` as a status line and holds
# it to the interim limit -- failing the turn for being long, which is the opposite of the intent.
# Mirrors TERMINAL_STOP_REASONS in libs/mngr_robinhood/imbue/mngr_robinhood/agent_runtime.py, which
# this container cannot import.
TERMINAL_STOP_REASONS = frozenset({"end_turn", "stop_sequence", "max_tokens"})


def _is_turn_ending(step: dict[str, Any]) -> bool:
    """Whether this message is the one the agent ended its turn with, as the workspace recorded it."""
    extra = step.get("extra")
    return isinstance(extra, dict) and extra.get("finish_reason") in TERMINAL_STOP_REASONS


def _does_document_record_endings(steps: list[dict[str, Any]]) -> bool:
    """Whether this document stamps a finish reason on its agent steps at all.

    This is the discriminator between the two shapes, and it must not be "did any turn end
    terminally": a trial cut short before it ever finished a turn records only `tool_use`, so asking
    for a terminal reason would call the workspace's own document markerless and fall back to
    position -- handing 300 words to the last status line of every turn, the exact case the marker
    exists to catch. The hand-built fallback stamps no finish reason anywhere.
    """
    return any(
        isinstance(step.get("extra"), dict) and "finish_reason" in step["extra"]
        for step in steps
        if step.get("source") == "agent"
    )


def _agent_turn_messages(trajectory_path: Path) -> tuple[list[list[tuple[int, bool]]], bool] | None:
    """Per agent turn, each of its messages in order as (word count, ends the turn), and whether the
    document records turn endings at all.

    A turn is the agent messages between one `user` step and the next. The greeting the agent gives
    before the client has spoken answers no turn and is left out, as are system steps and the
    tool-only inferences that carry no message."""
    steps = _trajectory_steps(trajectory_path)
    if steps is None:
        return None
    turns: list[list[tuple[int, bool]]] = []
    current: list[tuple[int, bool]] = []
    is_client_turn_seen = False
    for step in steps:
        source = step.get("source")
        message = str(step.get("message") or "").strip()
        if source == "user":
            if current:
                turns.append(current)
            current = []
            is_client_turn_seen = True
        elif source == "agent" and message and is_client_turn_seen:
            current.append((len(message.split()), _is_turn_ending(step)))
        else:
            # System steps and tool-only inferences are not client-facing speech, and the greeting
            # the agent gives before the client speaks is not a turn.
            continue
    if current:
        turns.append(current)
    return turns, _does_document_record_endings(steps)


def is_turn_within_limits(messages: list[tuple[int, bool]], does_document_record_endings: bool) -> bool:
    """Whether one turn's messages keep the limit for their role: its answer, or a status line.

    ``does_document_record_endings`` says whether the document stamps finish reasons at all, which is
    what tells the two markerless turns apart. In a document that stamps them, a turn with no terminal
    one is a turn the agent never ended -- the trial stopped mid-turn -- so it has no answer and every
    message in it is a status line. A document that stamps none is the driver's hand-built fallback,
    where a turn is one merged message and that message is the answer.
    """
    if not messages:
        return False
    ending_indices = [index for index, (_, is_ending) in enumerate(messages) if is_ending]
    if ending_indices:
        final_index = ending_indices[-1]
    elif does_document_record_endings:
        final_index = None
    else:
        final_index = len(messages) - 1
    return all(
        count <= (FINAL_MESSAGE_WORD_LIMIT if index == final_index else INTERIM_MESSAGE_WORD_LIMIT)
        for index, (count, _) in enumerate(messages)
    )


@criterion
def message_lengths_within_limits(workspace: Path) -> float:
    """The fraction of agent turns whose messages stay within the limit for their role."""
    measured = _agent_turn_messages(TRAJECTORY_PATH)
    if measured is None:
        return 0.0
    turns, does_document_record_endings = measured
    if not turns:
        return 0.0
    return sum(1 for turn in turns if is_turn_within_limits(turn, does_document_record_endings)) / len(turns)
