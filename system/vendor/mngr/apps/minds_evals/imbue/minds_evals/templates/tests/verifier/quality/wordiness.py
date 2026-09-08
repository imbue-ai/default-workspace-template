"""The wordiness guard (the negated-criterion idiom: it scores the behavior the agent should NOT
exhibit): passes unless the average words per agent turn exceeds the configured baseline by more
than 10%. It is one of the four equal-weight `quality` criteria (alongside the three judge
dimensions), not a hard gate; the driver records its own per-turn average in agent_result.metadata.
Runs in the verifier container: stdlib + rewardkit only, absolute paths."""

import json
from pathlib import Path
from typing import Any

from rewardkit import criterion

TRAJECTORY_PATH = Path("/logs/agent/trajectory.json")
CASE_PATH = Path("/tests/case.json")

WORDINESS_HEADROOM = 1.1


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


def _agent_turn_word_counts(trajectory_path: Path) -> list[int] | None:
    """Words in each agent turn: the agent messages between one `user` step and the next, merged.
    On the workspace's own document that spans every inference of the turn, and its opening
    greeting before any `user` step answers no turn; on the driver's hand-built fallback it is the
    single merged step the driver already wrote."""
    steps = _trajectory_steps(trajectory_path)
    if steps is None:
        return None
    counts: list[int] = []
    turn_messages: list[str] = []
    is_client_turn_seen = False
    for step in steps:
        source = step.get("source")
        message = str(step.get("message") or "").strip()
        if source == "user":
            if turn_messages:
                counts.append(len(" ".join(turn_messages).split()))
            turn_messages = []
            is_client_turn_seen = True
        elif source == "agent" and message and is_client_turn_seen:
            turn_messages.append(message)
        else:
            # System steps and tool-only inferences are not client-facing speech, and the greeting
            # the agent gives before the client speaks is not a turn.
            continue
    if turn_messages:
        counts.append(len(" ".join(turn_messages).split()))
    return counts


@criterion
def average_words_per_turn(workspace: Path) -> bool:
    """Average words per agent turn stays within 110% of the case's configured baseline."""
    counts = _agent_turn_word_counts(TRAJECTORY_PATH)
    if not counts:
        return False
    try:
        case = json.loads(CASE_PATH.read_text())
        baseline = float(case["avg_word_count_baseline"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    average = sum(counts) / len(counts)
    return average <= baseline * WORDINESS_HEADROOM
