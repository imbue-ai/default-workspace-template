"""Render the message-by-message transcript the judges score, at GRADE time, from the trial's ATIF
trajectory. The quality judge grades conciseness per individual agent message, so it must see each
agent step as its own block -- on the workspace's own document that is one block per inference, on
the driver's hand-built fallback one block per turn.

Running here (a verifier pre-step, before rewardkit) rather than in the driver means the rendering is
rebuilt from ``/logs/agent/trajectory.json`` on every grade, so ``harbor trial regrade`` re-scores
captured trials under the current rendering with no conversation re-run.

The rendering keeps only the real back-and-forth: one ``[USER]`` block per ``user`` step, one
``[AGENT · message N]`` block per ``agent`` step with a non-empty message (N running across the whole
conversation), and one ``[PROGRESS · ...]`` block per progress-view step record the agent declared or
closed. A message's inline images are reduced to ``[image: <alt>]``, because the chat renders them as
pictures and the markdown path around them is markup the client never reads. ``system`` steps
(framework-injected text, compaction summaries) and steps that carry no message are not client-facing
speech and are omitted. Runs in the verifier container: stdlib only, absolute paths.

Progress-view records are the second thing the client reads. The agent manages its work with `tk`,
whose ``--step`` records render as nodes on the chat's progress timeline: a title when the step is
declared and a one-line summary when it closes. Both are user-facing copy under the same plain-language
rule as the messages, and neither appears in any ``message`` field, so a transcript built from messages
alone leaves that copy ungraded. They are read from `tk`'s own machine-readable output rather than by
parsing the shell command: ``Created wor-step-<id>: <title>`` for a declaration and the
``tk-step <id> title:`` / ``tk-step <id> summary:`` pair `tk close` prints. Both are keyed to the step
record's own identity -- the ``wor-step-`` id shape, and the ``tk-step`` marker -- so cross-agent
ticket text, which is not written for the client, stays out of the judged transcript even when a
single inference creates a step record and a ticket together. The lines being read are these::

    Created wor-step-qa7d: Set up the to-do app so it can open as a tab
    Updated wor-step-qa7d -> closed
    tk-step wor-step-qa7d title: Set up the to-do app so it can open as a tab
    tk-step wor-step-qa7d summary: Set up the app's plumbing so it runs and can appear as a tab.

Only the top-level ``steps`` array is walked, never the ``subagent_trajectories`` embedded in it: a
subagent's steps are its own bookkeeping and never reach the client's timeline, so grading the client-
facing copy on them would charge the agent for text the client never saw.
"""

import json
import re
from pathlib import Path
from typing import Any

TRAJECTORY_PATH = Path("/logs/agent/trajectory.json")
JUDGE_TRANSCRIPT_PATH = Path("/logs/agent/judge_transcript.txt")
PROGRESS_SUMMARY_PATH = Path("/logs/agent/progress_summary.json")
# The tk verbs that put a node on the client's timeline. Their presence in a command is what says
# the agent meant to declare progress, which is the only way to tell "declared nothing" from
# "declared something this renderer failed to read".
TK_STEP_COMMAND_PATTERN = re.compile(r"\b(?:tk|ticket)\s+(?:super\s+)?(?:create\b[^|;&]*--step|start|close)\b")

# `tk create --step` prints one of these per step it made. The id is the progress node's identity, and
# the literal `-step-` segment is the discriminator: a step record is `<prefix>-step-<suffix>` where a
# regular ticket is `<prefix>-<suffix>`. The prefix itself is derived from the working directory's
# name at creation time, so it is NOT fixed -- `wor-` only comes from a directory called `workspace`,
# and pinning it here silently drops every step record from a run rooted anywhere else. This mirrors
# the workspace's own progress-view parser (`system_interface/.../turn-grouping.ts`), which is the
# authority on the shape. Keying on the id rather than on `--step` appearing somewhere in the command
# is what keeps a batched call -- one that makes a step record and opens a cross-agent ticket in the
# same inference -- from rendering the ticket's engineer-facing title as progress-view copy.
STEP_CREATED_PATTERN = re.compile(r"^Created (\S+-step-[a-z0-9]+): (.+)$", re.MULTILINE)
# `tk start` and `tk close` print these markers for a step record, and nothing like them for a regular
# ticket. A `title:` line is a declaration in its own right, not only a label for a close: `tk start`
# prints one, and the client sees that title on the timeline whether or not the step is ever closed.
STEP_TITLE_PATTERN = re.compile(r"^tk-step (\S+) title: (.*)$", re.MULTILINE)
STEP_SUMMARY_PATTERN = re.compile(r"^tk-step (\S+) summary: (.*)$", re.MULTILINE)
# The id shape that tells a step record from a regular ticket, for lines that carry an id but no
# `-step-` guarantee of their own.
STEP_ID_PATTERN = re.compile(r"^\S+-step-[a-z0-9]+$")
# The historical input fallback, for the form the workspace's own gate blesses:
# `S1=$(tk create --step "Set it up")` captures the id into a shell variable, so the output carries the
# id with no `Created <id>: <title>` line and the title survives only in the command text.
CREATE_TITLE_PATTERN = re.compile(r"\b(?:tk|ticket)\s+(?:super\s+)?create\b[^\"']*?(?:\"([^\"]*)\"|'([^']*)')")
CLOSE_SUMMARY_PATTERN = re.compile(r"\b(?:tk|ticket)\s+(?:super\s+)?close\s+(\S+)\s+(?:\"([^\"]*)\"|'([^']*)')")
# Any token in the output that is a step id, for zipping create titles onto positionally.
STEP_ID_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
# An inline image in a message: the chat renders it as a picture, so the client reads the picture and
# never the markdown around it.
IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\([^)]*\)")


def _image_placeholder(match: re.Match[str]) -> str:
    alt_text = match.group(1).strip()
    return "[image: {}]".format(alt_text) if alt_text else "[image]"


def _as_the_client_reads_it(message: str) -> str:
    """A message with its inline images reduced to what the client actually sees: a picture.

    The workspace path inside an image reference is markup, not something the agent said, and a judge
    reading the raw source scores it as a file path shown to a non-technical client -- a deduction for
    a leak that only exists in the transcript. Keeping the alt text preserves the fact that a picture
    was shown, and what it was of, which is the part the client did read.
    """
    return IMAGE_PATTERN.sub(_image_placeholder, message)


# `tk` runs in a shell, so its records only ever come back on a shell tool's output. Every other tool
# returns content the agent asked *for* rather than something it did: a `Read` of a log or of a saved
# transcript, a subagent's prose. Those routinely quote `Created <id>-step-<suffix>: ...` lines that
# the agent did not write and the client never saw, and rendering them as the agent's progress-view
# copy grades it on someone else's words.
# A tool name is matched exactly as the trajectory records it, so each harness's spelling of the
# shell is listed: claude calls it `Bash`, pi-coding calls it `bash`.
EXECUTING_TOOLS: frozenset[str] = frozenset({"Bash", "BashOutput", "bash"})


def _observation_text(step: dict[str, Any]) -> str:
    """What this step's executing tools printed, in one string, for the marker scan.

    See EXECUTING_TOOLS for why the rest of a step's output is excluded.
    """
    observation = step.get("observation")
    if not isinstance(observation, dict):
        return ""
    results = observation.get("results")
    if not isinstance(results, list):
        return ""
    calls = [call for call in (step.get("tool_calls") or []) if isinstance(call, dict)]
    tool_by_call_id = {call.get("tool_call_id"): call.get("function_name") for call in calls}
    return "\n".join(
        str(result.get("content") or "")
        for result in results
        if isinstance(result, dict) and tool_by_call_id.get(result.get("source_call_id")) in EXECUTING_TOOLS
    )


def _shell_commands(step: dict[str, Any]) -> list[str]:
    """The commands this step's executing tools ran, for the input fallback below."""
    commands = []
    for call in step.get("tool_calls") or []:
        if isinstance(call, dict) and call.get("function_name") in EXECUTING_TOOLS:
            arguments = call.get("arguments")
            if isinstance(arguments, dict):
                commands.append(str(arguments.get("command") or ""))
    return commands


def _titles_from_command_text(step: dict[str, Any], text: str) -> dict[str, str]:
    """Step titles recoverable only from the command, keyed to the ids the output echoed.

    `S1=$(tk create --step "Set it up")` -- the form the workspace's own gate blesses -- captures the
    id into a shell variable, so the output carries the bare id and no `Created <id>: <title>` line.
    The id and the title pair only by order, so the zip is refused unless the counts match exactly:
    a mismatch means the mapping is a guess, and a guessed title is worse than a missing one. Only
    step ids are considered, so a `tk create` of a regular ticket in the same command contributes
    nothing -- its engineer-facing title must never render as the client's copy.
    """
    titles = [double or single for double, single in CREATE_TITLE_PATTERN.findall(" ".join(_shell_commands(step)))]
    if not titles:
        return {}
    step_ids = [token for token in STEP_ID_TOKEN_PATTERN.findall(text) if STEP_ID_PATTERN.match(token)]
    if len(step_ids) != len(titles):
        return {}
    return {step_id: title.strip() for step_id, title in zip(step_ids, titles, strict=False)}


def render_progress_blocks(step: dict[str, Any], title_by_step_id: dict[str, str]) -> list[str]:
    """The progress-view blocks this inference produced: every step declared, then every step closed.

    One inference rarely does both, so this is the client's order in practice; where it does both, the
    declarations still read first.

    A declaration is any line that first names a step's title -- `Created <id>: <title>` from
    `tk create --step`, or the `tk-step <id> title:` that `tk start` prints -- because the client sees
    that title on the timeline from then on, whether or not the step is ever closed. Only the first
    such line for a step declares it; later ones relabel a node the client already has.

    ``title_by_step_id`` carries the titles forward across inferences, so a closed step's block names
    the step the client watched rather than just its summary line.
    """
    text = _observation_text(step)
    if not text:
        return []
    blocks: list[str] = []
    declared: list[tuple[str, str]] = list(STEP_CREATED_PATTERN.findall(text))
    declared.extend(STEP_TITLE_PATTERN.findall(text))
    declared.extend(_titles_from_command_text(step, text).items())
    for step_id, title in declared:
        if step_id in title_by_step_id:
            continue
        title_by_step_id[step_id] = title.strip()
        blocks.append("[PROGRESS \u00b7 step declared]\n{}".format(title.strip()))
    for step_id, summary in STEP_SUMMARY_PATTERN.findall(text):
        lines = ["[PROGRESS \u00b7 step done]"]
        title = title_by_step_id.get(step_id, "")
        if title:
            lines.append(title)
        lines.append(summary.strip())
        blocks.append("\n".join(lines))
    return blocks


def render_judge_transcript(steps: list[dict[str, Any]]) -> str:
    """The judged transcript for the given ATIF steps: ``[USER]`` blocks for client turns,
    ``[AGENT · message N]`` blocks for each non-empty agent message, and ``[PROGRESS · ...]`` blocks
    for the progress-view step records, all in the order the client saw them."""
    blocks: list[str] = []
    agent_message_index = 0
    title_by_step_id: dict[str, str] = {}
    for step in steps:
        source = step.get("source")
        message = str(step.get("message") or "").strip()
        if source == "agent":
            if message:
                agent_message_index += 1
                blocks.append("[AGENT · message {}]\n{}".format(agent_message_index, _as_the_client_reads_it(message)))
            blocks.extend(render_progress_blocks(step, title_by_step_id))
        elif source == "user" and message:
            blocks.append("[USER]\n{}".format(message))
        else:
            # A system step is the framework speaking, never the client or the agent.
            continue
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def load_trajectory_steps(path: Path) -> list[dict[str, Any]]:
    """The steps of the ATIF document at ``path``; empty when the file is absent or not a document."""
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(document, dict):
        return []
    steps = document.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def summarize_progress(steps: list[dict[str, Any]], rendered: str) -> dict[str, Any]:
    """What the timeline scan found, for the structural gate to check against.

    A trial that declared no steps and one whose parser broke both render an empty timeline, and the
    judge scores an empty timeline 10 -- so without this the second is indistinguishable from the
    first and a parsing break reads as perfect copy. Recording whether any tk step verb ran alongside
    the blocks recovered is what separates them.
    """
    return {
        "rendered_block_count": rendered.count("[PROGRESS \u00b7 "),
        "is_step_command_run": any(
            TK_STEP_COMMAND_PATTERN.search(command) is not None for step in steps for command in _shell_commands(step)
        ),
    }


def main() -> None:
    steps = load_trajectory_steps(TRAJECTORY_PATH)
    rendered = render_judge_transcript(steps)
    JUDGE_TRANSCRIPT_PATH.write_text(rendered)
    PROGRESS_SUMMARY_PATH.write_text(json.dumps(summarize_progress(steps, rendered), indent=2))


if __name__ == "__main__":
    main()
