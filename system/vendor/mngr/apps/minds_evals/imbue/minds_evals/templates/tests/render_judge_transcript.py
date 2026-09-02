"""Render the message-by-message transcript the judge scores, at GRADE time, from the raw event
stream. The judge grades conciseness per individual agent message, so it must see each assistant
message as its own block -- not the driver's per-turn merge, which glues several short status
messages into one apparent wall of text.

Running here (a verifier pre-step, before rewardkit) rather than in the driver means the rendering is
rebuilt from ``/logs/agent/full_transcript.jsonl`` on every grade, so ``harbor trial regrade``
re-scores captured trials under the current rendering with no conversation re-run.

The rendering keeps only the real back-and-forth: one ``[USER]`` block per real client turn (dropping
the ``/welcome`` trigger and the ``is_meta`` skill-body ingestions) and one ``[AGENT · message N]``
block per non-empty assistant message (N running across the whole conversation). Tool calls, tool
results, empty assistant events, and the driver's appended ``decider_message`` audit events carry no
client-facing message and are omitted. Runs in the verifier container: stdlib only, absolute paths.

Both common-transcript vintages appear in captured trials and both are rendered: the ATIF-shaped
``step`` records (discriminated by ``source``, text in ``message``) that mngr's emitters write, and
the legacy ``user_message``/``assistant_message`` records the workspace system_interface produces.
The framework noise the legacy shape flagged with ``is_meta`` arrives as a ``system`` step in the
ATIF shape, which is dropped for not being a client turn at all.
"""

import json
from pathlib import Path
from typing import Any

TRANSCRIPT_PATH = Path("/logs/agent/full_transcript.jsonl")
JUDGE_TRANSCRIPT_PATH = Path("/logs/agent/judge_transcript.txt")

# The framework's opening slash command (its own user_message), never a real
# client turn, so it is dropped from the judged transcript.
WELCOME_COMMAND = "/welcome"


def _client_turn_text(event: dict[str, Any]) -> str:
    """A real client turn's text, or "" for anything else (either stream vintage).

    Skill-body ingestions (the whole skill file replayed as a user turn) and the /welcome trigger
    are framework noise, not client speech.
    """
    if event.get("type") == "step" and event.get("source") == "user":
        content = str(event.get("message") or "").strip()
    elif event.get("type") == "user_message" and not event.get("is_meta"):
        content = str(event.get("content") or "").strip()
    else:
        return ""
    return "" if content == WELCOME_COMMAND else content


def _agent_message_text(event: dict[str, Any]) -> str:
    """One agent message's client-facing text, or "" for anything else (either stream vintage).

    Empty-text agent events are pure tool/internal turns and render nothing.
    """
    if event.get("type") == "step" and event.get("source") == "agent":
        return str(event.get("message") or "").strip()
    if event.get("type") == "assistant_message":
        return str(event.get("text") or "").strip()
    return ""


def render_judge_transcript(events: list[dict[str, Any]]) -> str:
    """The judged transcript for the given raw event stream: ``[USER]`` blocks for real client turns
    and ``[AGENT · message N]`` blocks for each non-empty agent message, blank-line separated."""
    blocks: list[str] = []
    agent_message_index = 0
    for event in events:
        client_text = _client_turn_text(event)
        if client_text:
            blocks.append("[USER]\n{}".format(client_text))
            continue
        # Each non-empty agent message is its own numbered block. Observations,
        # system steps, legacy tool_results, decider_message audit events, and any
        # other type carry nothing the client would see.
        agent_text = _agent_message_text(event)
        if agent_text:
            agent_message_index += 1
            blocks.append("[AGENT · message {}]\n{}".format(agent_message_index, agent_text))
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
