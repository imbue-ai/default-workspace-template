"""Structural gates for one trial: the trajectory parses, every entry completed, the run did not time
out, and the agent actually engaged (distinct, non-stub replies). All must pass for the trial to
earn a nonzero reward (finalize.py zeroes the reward otherwise). Runs in the verifier container:
stdlib + rewardkit only, absolute paths."""

import json
import re
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from rewardkit import criterion

# The trial's ATIF trajectory: the driver's hand-built turn summary, or the workspace agent's own
# document once the evidence phase captured it. The agent's replies are its `agent` steps.
TRAJECTORY_PATH = Path("/logs/agent/trajectory.json")
STATE_PATH = Path("/logs/agent/state.json")
CASE_PATH = Path("/tests/case.json")

# An agent that never authenticated (or is otherwise wedged) answers every turn
# with the same short stub. Matched against the WHOLE reply (fullmatch), so a
# real reply that merely mentions logging in is not caught -- only a reply that
# is essentially nothing but the stub (up to ~80 trailing chars of punctuation
# or a "please run /login" tail).
_STUB_REPLY_PATTERN = re.compile(
    r"\s*(not logged in|please run /login|invalid api key)[\s.·:!-]*(please run /login)?[\s.·:!-]{0,80}",
    re.IGNORECASE,
)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _trajectory_steps(trajectory_path: Path) -> list[dict[str, Any]] | None:
    """The document's steps, or None when there is no readable ATIF document at all."""
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


def _agent_replies(trajectory_path: Path) -> list[str] | None:
    """The non-empty agent messages that follow the client's first turn. On the workspace's own
    document the agent greets before the client speaks; that greeting answers nothing and must not
    count as a reply."""
    steps = _trajectory_steps(trajectory_path)
    if steps is None:
        return None
    replies: list[str] = []
    is_client_turn_seen = False
    for step in steps:
        source = step.get("source")
        message = str(step.get("message") or "").strip()
        if source == "user":
            is_client_turn_seen = True
        elif source == "agent" and message and is_client_turn_seen:
            replies.append(message)
        else:
            # System steps, tool-only inferences, and the greeting the agent gives before the
            # client speaks answer no client turn.
            continue
    return replies


@criterion
def transcript_has_agent_reply(workspace: Path) -> bool:
    """The trajectory exists, parses as an ATIF document, and carries at least one non-empty agent
    message after the client's first turn."""
    replies = _agent_replies(TRAJECTORY_PATH)
    return bool(replies)


# Every reason an entry can stop for. A budget-exhausted entry is a completed entry: the client
# asked as many times as it was allowed to and the agent did not satisfy it, which is a measurement,
# not a broken trial. Only an entry with no outcome at all -- one the
# conversation never reached -- fails the gate.
_ENTRY_OUTCOMES = frozenset({"completed", "satisfied", "budget_exhausted", "fallback"})


def _as_count(value: Any) -> int | None:
    """A recorded count, or None when the value is not one. `bool` is excluded deliberately:
    it is an `int` subclass, so `True` would otherwise read back as a count of 1."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def messages_sent(state: Mapping[str, Any] | None) -> int | None:
    """How many client messages the run actually sent, or None when state.json does not say."""
    if state is None:
        return None
    return _as_count(state.get("waits_done"))


def is_agent_engaged_substantively(replies: Sequence[str], sent_count: int) -> bool:
    if not replies or sent_count < 1:
        return False
    if all(_STUB_REPLY_PATTERN.fullmatch(reply) for reply in replies):
        return False
    # Keyed off the messages sent, not the replies: a run that sent one message can only be asked
    # for one distinct reply, but a run that sent several and got the same line back every time is
    # the wedged agent this gate exists to catch, however few of those lines were non-empty.
    return len(set(replies)) >= min(2, sent_count)


def is_every_turn_completed_without_entries(state: Mapping[str, Any], prompt_count: int) -> bool:
    """The rule for a state.json written before per-entry records existed: every configured entry
    sent exactly one message, and all of them were sent.

    Such a run had no goal entries, so its two counts are equal by construction. A regrade of a
    rollout captured back then has to be able to say so: reading the missing key as a failed
    conversation would report a schema gap as an agent that never finished talking.
    """
    return (
        state.get("test_state") == "finished"
        and messages_sent(state) == prompt_count
        and _as_count(state.get("num_turns")) == prompt_count
    )


def is_every_entry_completed(state: Mapping[str, Any] | None, case: Mapping[str, Any] | None) -> bool:
    if state is None or case is None:
        return False
    prompts: list[Any] = case.get("prompts") or []
    prompt_count = len(prompts)
    entries = state.get("entries")
    # CLEANUP: drop this branch, and `is_every_turn_completed_without_entries` with it, once no
    # rollout predating per-entry records is still regraded. Absent is the only shape that takes it:
    # an `entries` that is present and unreadable is a defect in the run, not an old artifact.
    if "entries" not in state:
        return is_every_turn_completed_without_entries(state, prompt_count)
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        return False
    entry_records: list[dict[str, Any]] = entries
    if len(entry_records) != prompt_count:
        return False
    if not all(entry.get("outcome") in _ENTRY_OUTCOMES for entry in entry_records):
        return False
    # Read through the same guard as `waits_done`: the two numbers are compared, so a record
    # this module cannot read as a count has to fail the gate rather than be summed as zero
    # (or raise out of the criterion).
    exchange_total = 0
    for entry, prompt in zip(entry_records, prompts, strict=False):
        exchange_count = _as_count(entry.get("exchange_count"))
        if exchange_count is None:
            return False
        # A string prompts entry is exactly one message, so any other count is a run that dropped or
        # repeated a deterministic turn -- a shape the totals alone would let another entry's surplus
        # hide. A goal entry has no such floor: a client the first reply already satisfied sends
        # nothing at all, which is the best outcome an entry can have.
        if isinstance(prompt, str) and exchange_count != 1:
            return False
        exchange_total += exchange_count
    sent_count = messages_sent(state)
    if sent_count is None:
        return False
    return state.get("test_state") == "finished" and sent_count == exchange_total


@criterion
def agent_engaged_substantively(workspace: Path) -> bool:
    """The agent produced distinct, non-stub replies -- not the same wedged/unauthenticated line
    every turn. Requires at least min(2, messages sent) distinct replies, and that not every reply
    is a known stub (e.g. 'Not logged in - Please run /login'), which would otherwise pass the other
    gates and earn a nonzero reward for an agent that never actually did anything. The bar follows
    the messages the client sent rather than the configured entries, since one goal entry can send
    several and can also be satisfied without sending any."""
    replies = _agent_replies(TRAJECTORY_PATH)
    sent_count = messages_sent(_load_json(STATE_PATH))
    if replies is None or sent_count is None:
        return False
    return is_agent_engaged_substantively(replies, sent_count)


@criterion
def all_turns_completed(workspace: Path) -> bool:
    """state.json reports every configured entry reached an outcome, and the messages the entries
    account for are exactly the messages that were sent."""
    return is_every_entry_completed(_load_json(STATE_PATH), _load_json(CASE_PATH))


@criterion
def not_timed_out(workspace: Path) -> bool:
    """The run did not exceed its wall-clock budget."""
    state = _load_json(STATE_PATH)
    if state is None:
        return False
    return state.get("test_state") != "timed_out" and not state.get("timed_out", False)
