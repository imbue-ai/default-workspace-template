"""Render the message-by-message transcript the judge scores, at GRADE time, from the raw event
stream. The judge grades conciseness per individual agent message, so it must see each assistant
message as its own block -- not the driver's per-turn merge, which glues several short status
messages into one apparent wall of text.

Running here (a verifier pre-step, before rewardkit) rather than in the driver means the rendering is
rebuilt from ``/logs/agent/full_transcript.jsonl`` on every grade, so ``harbor trial regrade``
re-scores captured trials under the current rendering with no conversation re-run.

The rendering keeps only the real back-and-forth: one ``[USER]`` block per real client turn (dropping
the ``/welcome`` trigger and the ``is_meta`` skill-body ingestions) and one ``[AGENT - message N]``
block per non-empty assistant message (N running across the whole conversation). Tool calls, tool
results, empty assistant events, and the driver's appended ``decider_message`` audit events carry no
client-facing message and are omitted. Runs in the verifier container: stdlib only, absolute paths.
"""

import json
from pathlib import Path
from typing import Any

TRANSCRIPT_PATH = Path("/logs/agent/full_transcript.jsonl")
JUDGE_TRANSCRIPT_PATH = Path("/logs/agent/judge_transcript.txt")

# The framework's opening slash command (its own user_message), never a real
# client turn, so it is dropped from the judged transcript.
WELCOME_COMMAND = "/welcome"


def render_judge_transcript(events: list[dict[str, Any]]) -> str:
    """The judged transcript for the given raw event stream: ``[USER]`` blocks for real client turns
    and ``[AGENT - message N]`` blocks for each non-empty agent message, blank-line separated."""
    blocks: list[str] = []
    agent_message_index = 0
    for event in events:
        event_type = event.get("type")
        if event_type == "user_message":
            # Skill-body ingestions (the whole skill file replayed as a user turn)
            # and the /welcome trigger are framework noise, not client speech.
            if event.get("is_meta"):
                continue
            content = (event.get("content") or "").strip()
            if content and content != WELCOME_COMMAND:
                blocks.append("[USER]\n{}".format(content))
            continue
        if event_type == "assistant_message":
            # Empty-text assistant events are pure tool/internal turns; only real
            # client-facing messages are rendered, each as its own numbered block.
            text = (event.get("text") or "").strip()
            if text:
                agent_message_index += 1
                blocks.append("[AGENT · message {}]\n{}".format(agent_message_index, text))
            continue
        # tool_result, decider_message, and any other event type carry nothing the
        # client would see, so they never reach the judged transcript.
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _load_transcript(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def main() -> None:
    JUDGE_TRANSCRIPT_PATH.write_text(render_judge_transcript(_load_transcript(TRANSCRIPT_PATH)))


if __name__ == "__main__":
    main()
