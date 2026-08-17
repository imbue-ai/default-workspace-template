"""The wordiness guard (the negated-criterion idiom: it scores the behavior the agent should NOT
exhibit): passes unless the average words per agent turn exceeds the configured baseline by more
than 10%. It is one of the four equal-weight `quality` criteria (alongside the three judge
dimensions), not a hard gate; the raw average is also recorded by the driver in
agent_result.metadata. Runs in the verifier container: stdlib + rewardkit only, absolute paths."""

import json
from pathlib import Path

from rewardkit import criterion

CONVERSATION_PATH = Path("/logs/agent/conversation.jsonl")
CASE_PATH = Path("/tests/case.json")

WORDINESS_HEADROOM = 1.1


def _agent_turn_word_counts() -> list[int] | None:
    try:
        lines = CONVERSATION_PATH.read_text().splitlines()
    except OSError:
        return None
    counts: list[int] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except ValueError:
            return None
        if isinstance(event, dict) and event.get("type") == "assistant_message":
            text = (event.get("text") or "").strip()
            if text:
                counts.append(len(text.split()))
    return counts


@criterion
def average_words_per_turn(workspace: Path) -> bool:
    """Average words per agent turn stays within 110% of the case's configured baseline."""
    counts = _agent_turn_word_counts()
    if not counts:
        return False
    try:
        case = json.loads(CASE_PATH.read_text())
        baseline = float(case["avg_word_count_baseline"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    average = sum(counts) / len(counts)
    return average <= baseline * WORDINESS_HEADROOM
