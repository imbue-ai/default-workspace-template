"""Render the message-by-message transcript the judges score, at GRADE time, from the trial's ATIF
trajectory. The quality judge grades conciseness per individual agent message, so it must see each
agent step as its own block -- on the workspace's own document that is one block per inference, on
the driver's hand-built fallback one block per turn.

Running here (a verifier pre-step, before rewardkit) rather than in the driver means the rendering is
rebuilt from ``/logs/agent/trajectory.json`` on every grade, so ``harbor trial regrade`` re-scores
captured trials under the current rendering with no conversation re-run.

The rendering keeps only the real back-and-forth: one ``[USER]`` block per ``user`` step and one
``[AGENT · message N]`` block per ``agent`` step with a non-empty message (N running across the whole
conversation). ``system`` steps (framework-injected text, compaction summaries) and steps that carry
no message (tool-only inferences) are not client-facing speech and are omitted. Runs in the verifier
container: stdlib only, absolute paths.
"""

import json
from pathlib import Path
from typing import Any

TRAJECTORY_PATH = Path("/logs/agent/trajectory.json")
JUDGE_TRANSCRIPT_PATH = Path("/logs/agent/judge_transcript.txt")


def render_judge_transcript(steps: list[dict[str, Any]]) -> str:
    """The judged transcript for the given ATIF steps: ``[USER]`` blocks for client turns and
    ``[AGENT · message N]`` blocks for each non-empty agent message, blank-line separated."""
    blocks: list[str] = []
    agent_message_index = 0
    for step in steps:
        message = str(step.get("message") or "").strip()
        if not message:
            continue
        source = step.get("source")
        if source == "user":
            blocks.append("[USER]\n{}".format(message))
        elif source == "agent":
            agent_message_index += 1
            blocks.append("[AGENT · message {}]\n{}".format(agent_message_index, message))
        else:
            # A system step is the framework speaking, never the client or the agent.
            continue
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def load_trajectory_steps(path: Path) -> list[dict[str, Any]]:
    """The steps of the ATIF document at ``path``; empty when the file is absent or not a document."""
    try:
        raw = path.read_text()
    except OSError:
        return []
    try:
        document = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(document, dict):
        return []
    steps = document.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def main() -> None:
    JUDGE_TRANSCRIPT_PATH.write_text(render_judge_transcript(load_trajectory_steps(TRAJECTORY_PATH)))


if __name__ == "__main__":
    main()
