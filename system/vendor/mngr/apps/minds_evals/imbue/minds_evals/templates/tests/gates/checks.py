"""Structural gates for one trial: the transcript parses, every turn completed, the run did not time
out, and the agent actually engaged (distinct, non-stub replies). All must pass for the trial to
earn a nonzero reward (finalize.py zeroes the reward otherwise). Runs in the verifier container:
stdlib + rewardkit only, absolute paths."""

import json
import re
from pathlib import Path
from typing import Any

from rewardkit import criterion

# The clean per-turn conversation (the eval's own user turns + agent replies),
# not the raw event stream: the driver writes it free of framework noise.
CONVERSATION_PATH = Path("/logs/agent/conversation.jsonl")
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


def _conversation_events() -> list[dict[str, Any]] | None:
    try:
        lines = CONVERSATION_PATH.read_text().splitlines()
    except OSError:
        return None
    events: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except ValueError:
            return None
        if isinstance(event, dict):
            events.append(event)
    return events


def _agent_replies() -> list[str] | None:
    events = _conversation_events()
    if events is None:
        return None
    return [
        (event.get("text") or "").strip()
        for event in events
        if event.get("type") == "assistant_message" and (event.get("text") or "").strip()
    ]


@criterion
def transcript_has_agent_reply(workspace: Path) -> bool:
    """The conversation file exists, parses as JSONL, and carries at least one non-empty agent reply."""
    replies = _agent_replies()
    return bool(replies)


@criterion
def agent_engaged_substantively(workspace: Path) -> bool:
    """The agent produced distinct, non-stub replies -- not the same wedged/unauthenticated line every
    turn. Requires at least min(2, turn count) distinct replies and that not every reply is a known
    stub (e.g. 'Not logged in - Please run /login'), which would otherwise pass the other gates and
    earn a nonzero reward for an agent that never actually did anything."""
    replies = _agent_replies()
    case = _load_json(CASE_PATH)
    if not replies or case is None:
        return False
    if all(_STUB_REPLY_PATTERN.fullmatch(reply) for reply in replies):
        return False
    turn_count = len(case.get("prompts") or [])
    return len(set(replies)) >= min(2, turn_count)


@criterion
def all_turns_completed(workspace: Path) -> bool:
    """state.json reports every configured turn finished."""
    state = _load_json(STATE_PATH)
    case = _load_json(CASE_PATH)
    if state is None or case is None:
        return False
    expected_turn_count = len(case.get("prompts") or [])
    return (
        state.get("test_state") == "finished"
        and state.get("waits_done") == expected_turn_count
        and state.get("num_turns") == expected_turn_count
    )


@criterion
def not_timed_out(workspace: Path) -> bool:
    """The run did not exceed its wall-clock budget."""
    state = _load_json(STATE_PATH)
    if state is None:
        return False
    return state.get("test_state") != "timed_out" and not state.get("timed_out", False)
