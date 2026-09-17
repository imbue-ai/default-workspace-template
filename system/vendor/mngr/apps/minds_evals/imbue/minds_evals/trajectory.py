import json
import re
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

from harbor.models.trajectories import Agent as TrajectoryAgent
from harbor.models.trajectories import FinalMetrics
from harbor.models.trajectories import Step
from harbor.models.trajectories import Trajectory
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals.data_types import SKILL_NAME_EXPRESSION
from imbue.minds_evals.data_types import SkillInvocation
from imbue.minds_evals.data_types import StepBoundary
from imbue.minds_evals.data_types import TrajectoryProvenance
from imbue.minds_evals.data_types import TrajectorySource
from imbue.minds_evals.data_types import WorkerLaunch
from imbue.minds_evals.data_types import WorkerState
from imbue.minds_evals.errors import TrajectoryDocumentError
from imbue.minds_evals.usage import TrialUsage
from imbue.mngr.agents.trajectory_build import MNGR_SUBAGENT_KIND
from imbue.mngr.agents.trajectory_build import TrajectoryEnrichment
from imbue.mngr.agents.trajectory_build import build_trajectory_from_records
from imbue.mngr.agents.trajectory_build import parse_stream_content
from imbue.mngr.errors import TrajectoryBuildError

# How the launch-task skill creates a worker: a Bash call to its script, or a bare `mngr create`. The
# --name is the only join between the launching step and the worker, since a Bash command cannot
# know its own tool_use_id and the worker carries no parent label. The skill's own snippet spells the
# call across backslash-continued lines, so the arguments run to the first newline that is not one.
_WORKER_LAUNCH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"create_worker\.py\s+(?:launch|launch-sync)\b(?P<arguments>(?:\\\n|[^\n])*)"
)
# argparse takes each option's value after whitespace or an `=`.
_LAUNCH_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"--name(?:=|\s+)(?P<name>\S+)")
_LAUNCH_TASK_FILE_PATTERN: Final[re.Pattern[str]] = re.compile(r"--task-file(?:=|\s+)(?P<task_file>\S+)")
_MNGR_CREATE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\bmngr\s+create\s+(?P<name>[^\s-]\S*)")
# codex runs its shell from inside "code mode": one tool call carries a whole JavaScript program that
# reaches the real tools as `tools.<fn>({...})`, so the command is a string literal inside that
# program rather than an argument of the call. One program may batch several calls, and the shell
# function is spelled two ways -- `tools.exec_command({cmd})` or `tools.shell_command({command})`,
# depending on the codex version and whether its unified exec is on -- so both are read. The
# surrounding program is arbitrary JavaScript rather than JSON, which is why the literal is read out
# with a regex instead of being parsed -- the same approach the chat app's own codex tool labels take.
# This parse is mirrored by the CODE_MODE_* patterns and commands_in in
# templates/tests/verifier/render_judge_transcript.py, which recovers the same command text inside the
# verifier container. The two cannot be shared: that file runs on stdlib and rewardkit alone, with no
# imbue package. Keep the two in step.
_CODE_MODE_CALL_PATTERN: Final[re.Pattern[str]] = re.compile(r"tools\.([A-Za-z_]\w*)\s*\(")
# Each shell function's own command key, followed by any of the three JavaScript string literal forms.
# A call is read under its function's key only: the other key can appear inside the command text
# itself (`python3 -c "print({'command': 'ls'})"`), and would match there. Each form consumes a
# backslash escape as a unit, so a command containing an escaped quote is captured whole rather than
# clipped there. A template literal keeps its `${...}` placeholders as written. The lookbehind keeps a
# longer key that merely ends in `cmd` from matching.
_CODE_MODE_COMMAND_PATTERN_BY_FUNCTION: Final[dict[str, re.Pattern[str]]] = {
    function: re.compile(
        r"(?<![\w$])[\"']?" + key + r"[\"']?\s*:\s*"
        r"(?:\"((?:\\.|[^\"\\])*)\"|'((?:\\.|[^'\\])*)'|`((?:\\.|[^`\\])*)`)"
    )
    for function, key in (("shell_command", "command"), ("exec_command", "cmd"))
}
# The JavaScript string escapes worth undoing in a captured command. An unknown escape keeps the
# character after the backslash, which is harmless here and cannot raise the way a decode can.
_JS_UNESCAPES: Final[dict[str, str]] = {
    '"': '"',
    "'": "'",
    "`": "`",
    "\\": "\\",
    "/": "/",
    "n": "\n",
    "t": "\t",
    "r": "\r",
}
# A `$NAME` or `${NAME}` anywhere in a value, and the `NAME=value` assignment earlier in the same
# command that gives it its worth (the skill's snippet writes the name and the task-file path with
# such a variable).
_SHELL_VARIABLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)
_SHELL_ASSIGNMENT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[\s;&|])(?:export\s+)?(?P<variable>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>[^\s;&|]+)"
)
_SHELL_QUOTES: Final[str] = "'\""

# claude's own skill tool, which names the skill in its arguments. Every other harness reads the
# skill file itself, so the invocation shows up as a path to the skill's body in what the call runs
# or opens: a shell command, the path a read tool is pointed at, or a codex code-mode program.
_SKILL_TOOL_NAME: Final[str] = "Skill"
_SKILL_FILE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"skills/(?P<name>{})/SKILL\.md".format(SKILL_NAME_EXPRESSION)
)
# Only these arguments are searched for that path, so a skill a call merely mentions -- in the
# content it writes, in a note it records -- is not read as a reach for it. `prompt` is left out on
# purpose too: a skill named in a delegation prompt is the worker's invocation, not the lead's.
_SKILL_PATH_ARGUMENT_KEYS: Final[tuple[str, ...]] = ("command", "cmd", "_raw", "file_path", "path")


# The `extra` tag every harness-written step carries, so a reader can tell the eval's own annotations
# apart from the agent's steps without matching on the prose.
STEP_BOUNDARY_KIND: Final[str] = "step_boundary"
# The first line of every boundary marker. The workspace's own transcript contributes `system`
# steps of its own -- skill bodies, tens of thousands of characters each -- and `harbor view`
# renders every system step as the same card, labelled only by a small "system" chip. The banner
# is what makes a harness boundary findable while scrolling a two-hundred-step trajectory.
STEP_BOUNDARY_BANNER: Final[str] = (
    "===================================== MINDS EVALS ======================================"
)


@pure
def _step_boundary_message(boundary: StepBoundary) -> str:
    """What a boundary marker says. The prose carries the disclaimer as well as the name: a reader who
    meets the step in `harbor view` sees only the message, never the `extra` tag beside it."""
    return "{}\n\nStep: {}\n\n(Written by the minds-evals. Not part of the conversation, and not graded.)".format(
        STEP_BOUNDARY_BANNER, boundary.name
    )


@pure
def _step_boundary_step(boundary: StepBoundary, step_id: int) -> Step:
    """One boundary as the ATIF step it becomes: `system`, which is the source every verifier reader
    skips, so the marker cannot reach a judge."""
    return Step(
        step_id=step_id,
        timestamp=boundary.started_at,
        source="system",
        message=_step_boundary_message(boundary),
        extra={"minds_evals": {"kind": STEP_BOUNDARY_KIND, "step_name": boundary.name}},
    )


@pure
def _document_boundary_position(
    raw_steps: Sequence[Mapping[str, Any]], boundary: StepBoundary, search_from: int
) -> int | None:
    """Where a boundary belongs in the workspace's own document, or None when it cannot be placed.

    The step's opening client message is the exact join: the driver sent that text verbatim, so it
    appears as a `user` step. Timestamps are the fallback, since the document is written inside the
    box while the boundary is stamped on the host, and the two clocks agree only to within their
    drift. A boundary that resolves to neither is dropped rather than guessed at: a marker in the
    wrong place misreads the conversation, while a missing one only leaves it undivided.
    """
    if boundary.opening_message:
        for index in range(search_from, len(raw_steps)):
            step = raw_steps[index]
            if step.get("source") == "user" and step.get("message") == boundary.opening_message:
                return index
    for index in range(search_from, len(raw_steps)):
        timestamp = raw_steps[index].get("timestamp")
        if isinstance(timestamp, str) and timestamp >= boundary.started_at:
            return index
    return None


@pure
def with_document_step_boundaries(
    raw_steps: Sequence[Mapping[str, Any]], boundaries: Sequence[StepBoundary]
) -> list[dict[str, Any]]:
    """The document's steps with a boundary marker spliced in ahead of each step's first turn.

    ATIF numbers steps sequentially from 1, so every step after an insertion is renumbered. Nothing
    else in the document refers to a step by its number -- a tool call is joined to its result by
    `source_call_id`, and an embedded worker to its launch by `tool_call_id` -- so renumbering moves
    no other reference.
    """
    if not boundaries:
        return [dict(step) for step in raw_steps]
    positions: list[tuple[int, StepBoundary]] = []
    search_from = 0
    for boundary in boundaries:
        position = _document_boundary_position(raw_steps, boundary, search_from)
        if position is None:
            logger.warning("Could not place the boundary for step {} in the workspace document", boundary.name)
            continue
        positions.append((position, boundary))
        search_from = position + 1
    marked: list[dict[str, Any]] = []
    boundaries_by_position: dict[int, list[StepBoundary]] = {}
    for position, boundary in positions:
        boundaries_by_position.setdefault(position, []).append(boundary)
    for index, step in enumerate(raw_steps):
        for boundary in boundaries_by_position.get(index, []):
            marked.append(_step_boundary_step(boundary, len(marked) + 1).model_dump(exclude_none=True, mode="json"))
        marked.append({**step, "step_id": len(marked) + 1})
    return marked


@pure
def conversation_steps_with_boundaries(
    conversation: Sequence[Mapping[str, str]], boundaries: Sequence[StepBoundary], timestamp: str
) -> list[Step]:
    """The hand-built shape's steps: one per non-empty conversation entry, with a boundary marker
    ahead of each step's first turn.

    Here the join is exact -- a boundary records how many kept entries preceded it -- so the steps are
    numbered once, as they are built. A boundary past the last entry belongs to a step that ended
    before the client said anything, and lands last.
    """
    steps: list[Step] = []
    pending = list(boundaries)
    for index, entry in enumerate(entry for entry in conversation if entry["text"].strip()):
        while pending and pending[0].conversation_index <= index:
            steps.append(_step_boundary_step(pending.pop(0), len(steps) + 1))
        steps.append(
            Step(
                step_id=len(steps) + 1,
                timestamp=timestamp,
                source="user" if entry["role"] == "user" else "agent",
                message=entry["text"],
            )
        )
    for boundary in pending:
        steps.append(_step_boundary_step(boundary, len(steps) + 1))
    return steps


@pure
def parse_transcript_jsonl(content: str) -> list[dict[str, Any]]:
    """The records of a captured common-transcript stream, in file order. A line that is not a JSON
    object is skipped: the stream is append-only and a crash mid-append leaves a truncated last line."""
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except ValueError as exc:
            logger.warning("Skipped line {} of a captured transcript stream, which is not JSON: {}", line_number, exc)
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


class EmbeddedWorker(FrozenModel):
    """One captured worker ready to embed: its document (its own workers already grafted in) and what the
    trial knows about it."""

    launch: WorkerLaunch = Field(description="The launch the worker answers to")
    document: dict[str, Any] = Field(description="The worker's ATIF document, as a JSON-shaped dict")
    agent_id: str = Field(
        description=(
            "The mngr agent id the capture resolved, empty when it resolved none. Distinct from the "
            "document's trajectory_id, which falls back to a launch-derived stand-in so an "
            "unidentified worker's evidence is still embedded."
        )
    )
    state: WorkerState = Field(description="The worker's state at collection time")
    report_path: str = Field(description="Bundle-relative path of the captured reports directory, or empty")


@pure
def _provenance_extra(provenance: TrajectoryProvenance, source: TrajectorySource) -> dict[str, Any]:
    """The eval's own root-level `extra` block, identical in shape on both trajectory sources."""
    return {
        "minds_evals": {
            "source": source.value,
            "driver": {"name": provenance.driver_name, "version": provenance.driver_version},
            "decider_model": provenance.decider_model,
            "decider_turns": [turn.model_dump(mode="json") for turn in provenance.decider_turns],
            "harbor_session_id": provenance.harbor_session_id,
            "case_id": provenance.case_id,
            "usage_source": provenance.usage_source.value,
            "arm": provenance.arm.model_dump(mode="json"),
        }
    }


@pure
def _resolved_final_metrics(workspace_usage: TrialUsage, total_steps: int) -> FinalMetrics | None:
    """The trial's resolved workspace usage in ATIF's aggregate shape, or None when the usage account
    saw no messages at all (which is not the same claim as zero)."""
    if not workspace_usage.message_count:
        return None
    return FinalMetrics(
        total_prompt_tokens=workspace_usage.n_input_tokens,
        total_completion_tokens=workspace_usage.tokens.output,
        total_cached_tokens=workspace_usage.n_cache_tokens,
        total_cost_usd=workspace_usage.cost_usd,
        total_steps=total_steps,
    )


@pure
def _document_object(document_json: str) -> dict[str, Any]:
    """The captured document's text as the JSON object it must be.

    Raises TrajectoryDocumentError otherwise.
    """
    try:
        raw_document = json.loads(document_json)
    except ValueError as exc:
        raise TrajectoryDocumentError("the captured trajectory document is not valid JSON") from exc
    if not isinstance(raw_document, dict):
        raise TrajectoryDocumentError("the captured trajectory document is not a JSON object")
    return raw_document


@pure
def parse_worker_document(document_json: str) -> dict[str, Any]:
    """A captured worker document, checked the way harbor will check it once embedded: valid ATIF with
    the `trajectory_id` every embedded subagent must carry. Refusing it here keeps one bad worker
    file from sinking the root document it would be grafted into.

    Raises TrajectoryDocumentError when it cannot be embedded.
    """
    document = _document_object(document_json)
    try:
        Trajectory.model_validate(document)
    except ValidationError as exc:
        raise TrajectoryDocumentError("the worker's captured document is not valid ATIF: {}".format(exc)) from exc
    if not document.get("trajectory_id"):
        raise TrajectoryDocumentError("the worker's captured document has no trajectory_id")
    return document


@pure
def build_workspace_trajectory(
    document_json: str,
    provenance: TrajectoryProvenance,
    workspace_usage: TrialUsage,
    workers: Sequence[EmbeddedWorker],
    boundaries: Sequence[StepBoundary],
) -> Trajectory:
    """The workspace's own ATIF document with the eval's reconciliations applied.

    `final_metrics` becomes the trial's resolved usage (the same figures harbor's agent_result
    carries), keeping the document's own per-step sums only when that account saw nothing;
    `extra.minds_evals` records what the document cannot know; the workers the agent launched are
    embedded under their launching calls. Every other field -- the steps, the identities, the
    subagents mngr embedded -- is the workspace's, untouched. The result is validated after the
    edits, so an invalid document is refused rather than written.

    Raises TrajectoryDocumentError when the captured document is not valid ATIF.
    """
    raw_document = _document_object(document_json)
    raw_steps = raw_document.get("steps")
    step_count = len(raw_steps) if isinstance(raw_steps, list) else 0
    # The agent's own steps, not the harness's markers: `total_steps` describes the work the
    # trajectory records, and a cosmetic step is none of it.
    resolved_metrics = _resolved_final_metrics(workspace_usage, step_count)
    raw_extra = raw_document.get("extra")
    grafted_document = graft_worker_trajectories(raw_document, workers)
    reconciled_document = {
        **grafted_document,
        "steps": with_document_step_boundaries(grafted_document.get("steps") or [], boundaries),
        "final_metrics": resolved_metrics.model_dump(exclude_none=True)
        if resolved_metrics is not None
        else raw_document.get("final_metrics"),
        "extra": {
            **(raw_extra if isinstance(raw_extra, dict) else {}),
            **_provenance_extra(provenance, TrajectorySource.WORKSPACE),
        },
    }
    try:
        return Trajectory.model_validate(reconciled_document)
    except ValidationError as exc:
        raise TrajectoryDocumentError("the captured trajectory document is not valid ATIF: {}".format(exc)) from exc


@pure
def _option_value(raw_value: str, preceding_command_text: str) -> str:
    """An option's value as the shell hands it to the script: surrounding quotes dropped, and each
    variable in it replaced by what the same command assigned it before the launch (an assignment's
    own value expanded against the assignments before it, as the shell expands it when it is made).
    A variable not assigned by then is kept as written, so the launch is still found and then
    recorded as one that could not be captured."""
    value = raw_value.strip(_SHELL_QUOTES)
    if _SHELL_VARIABLE_PATTERN.search(value) is None:
        return value
    assigned_values: dict[str, str] = {}
    for match in _SHELL_ASSIGNMENT_PATTERN.finditer(preceding_command_text):
        assigned_values[match.group("variable")] = _expand_shell_variables(
            match.group("value").strip(_SHELL_QUOTES), assigned_values
        )
    return _expand_shell_variables(value, assigned_values)


@pure
def _expand_shell_variables(value: str, assigned_values: Mapping[str, str]) -> str:
    """The value with each `$NAME`/`${NAME}` replaced by its assignment, one never assigned kept as written."""
    pieces: list[str] = []
    position = 0
    for match in _SHELL_VARIABLE_PATTERN.finditer(value):
        variable = match.group("braced") or match.group("bare")
        pieces.append(value[position : match.start()])
        pieces.append(assigned_values.get(variable, match.group(0)))
        position = match.end()
    pieces.append(value[position:])
    return "".join(pieces)


@pure
def _launched_worker(command: str) -> tuple[str, str] | None:
    """(name, task file) of the worker a command launches, or None when it launches none."""
    launch_match = _WORKER_LAUNCH_PATTERN.search(command)
    if launch_match is not None:
        preceding_text = command[: launch_match.start()]
        name_match = _LAUNCH_NAME_PATTERN.search(launch_match.group("arguments"))
        if name_match is None:
            return None
        task_file_match = _LAUNCH_TASK_FILE_PATTERN.search(launch_match.group("arguments"))
        return (
            _option_value(name_match.group("name"), preceding_text),
            _option_value(task_file_match.group("task_file"), preceding_text) if task_file_match is not None else "",
        )
    create_match = _MNGR_CREATE_PATTERN.search(command)
    if create_match is not None:
        return _option_value(create_match.group("name"), command[: create_match.start()]), ""
    return None


@pure
def _unescaped_js_string(value: str) -> str:
    """A captured JavaScript string literal with its escapes undone."""
    if "\\" not in value:
        return value
    characters: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            characters.append(_JS_UNESCAPES.get(value[index + 1], value[index + 1]))
            index += 2
        else:
            characters.append(value[index])
            index += 1
    return "".join(characters)


@pure
def _code_mode_commands(program: str) -> list[str]:
    """Every shell command a code-mode program runs, in the order the program runs them.

    Each call's arguments are read from the slice between its own ``tools.`` and the next one, so a
    program that batches a shell call behind another tool's call reads the command belonging to the
    shell call rather than the first command literal anywhere in the text.
    """
    calls = list(_CODE_MODE_CALL_PATTERN.finditer(program))
    commands: list[str] = []
    for index, call in enumerate(calls):
        pattern = _CODE_MODE_COMMAND_PATTERN_BY_FUNCTION.get(call.group(1))
        if pattern is None:
            continue
        end = calls[index + 1].start() if index + 1 < len(calls) else len(program)
        match = pattern.search(program[call.end() : end])
        if match is not None:
            commands.append(_unescaped_js_string(next(group for group in match.groups() if group is not None)))
    return commands


@pure
def _shell_commands_in_call(tool_call: Mapping[str, Any]) -> list[str]:
    """The shell commands one tool call runs, whichever shape its harness uses.

    claude and pi-coding pass the command as an argument of the call; codex passes a code-mode
    program under ``_raw`` and runs the shell from inside it.
    """
    arguments = tool_call.get("arguments")
    if not isinstance(arguments, Mapping):
        return []
    for key in ("command", "cmd"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return [value]
    program = arguments.get("_raw")
    return _code_mode_commands(program) if isinstance(program, str) else []


@pure
def scan_worker_launches(steps: Sequence[Mapping[str, Any]], depth: int, lead_name: str) -> list[WorkerLaunch]:
    """The workers an agent's steps launched, in launch order, one per name.

    Reads either a captured stream's ``step`` records or a document's ``steps``: both carry the
    agent's tool calls with their complete ``arguments``. A name launched twice yields one entry, for
    its first launch, since one agent answers to the name at collection time.

    One call can launch several workers, because a codex code-mode program batches several shell
    calls into one tool call. They all take that call's id, which is what the embedding step attaches
    them by, and it holds a list of refs per result for exactly that reason.
    """
    launches: list[WorkerLaunch] = []
    seen_names: set[str] = set()
    for step in steps:
        if step.get("source") != "agent":
            continue
        for tool_call in step.get("tool_calls") or []:
            if not isinstance(tool_call, Mapping):
                continue
            for command in _shell_commands_in_call(tool_call):
                launched = _launched_worker(command)
                if launched is None or launched[0] in seen_names:
                    continue
                seen_names.add(launched[0])
                launches.append(
                    WorkerLaunch(
                        name=launched[0],
                        tool_call_id=str(tool_call.get("tool_call_id") or ""),
                        task_file=launched[1],
                        depth=depth,
                        lead_name=lead_name,
                    )
                )
    return launches


@pure
def _skills_invoked_by_call(tool_call: Mapping[str, Any]) -> list[str]:
    """The skills one tool call invokes, in the order the call names them, each once.

    claude invokes a skill through its `Skill` tool, which names it outright. The other harnesses
    have no such tool: the agent reads the skill's own file, so the invocation is a path to that
    file in what the call runs or opens (`_SKILL_PATH_ARGUMENT_KEYS`).
    """
    arguments = tool_call.get("arguments")
    if not isinstance(arguments, Mapping):
        return []
    if str(tool_call.get("function_name") or "") == _SKILL_TOOL_NAME:
        named = str(arguments.get("skill") or "").strip()
        if named:
            return [named]
    found = (
        match.group("name")
        for key in _SKILL_PATH_ARGUMENT_KEYS
        if isinstance(value := arguments.get(key), str)
        for match in _SKILL_FILE_PATTERN.finditer(value)
    )
    return list(dict.fromkeys(found))


@pure
def _errored_call_ids(records: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """The tool calls whose result came back an error, in whichever shape the record carries it: a
    captured stream writes observations as records of their own, a built document nests them under
    the step that made the call. A result with no call id names no call, so it is left out."""
    errored: set[str] = set()
    for record in records:
        observation = record.get("observation")
        nested = observation.get("results") if isinstance(observation, Mapping) else None
        for result in record.get("results") or nested or []:
            if not isinstance(result, Mapping):
                continue
            extra = result.get("extra")
            call_id = str(result.get("source_call_id") or "")
            if call_id and isinstance(extra, Mapping) and extra.get("is_error"):
                errored.add(call_id)
    return frozenset(errored)


@pure
def scan_skill_invocations(steps: Sequence[Mapping[str, Any]]) -> tuple[SkillInvocation, ...]:
    """Every skill an agent's steps invoked, in invocation order.

    Reads either a captured stream's ``step`` records or a document's ``steps``, the way
    ``scan_worker_launches`` does. Repeats are kept: a case that asks what the agent reached for
    wants each reach, and a reader of the manifest entry sees where each one happened.

    A call whose result came back an error invoked nothing, and is left out: a `Skill` call answered
    `Unknown skill` never ran the skill (an unresolved plugin, which `harness_quality` counts as a
    broken workspace), and a command that failed never opened the file. Counting those would score a
    broken workspace as an agent that worked as asked, and charge an agent for a forbidden skill it
    never got. The veto is per call, not per command, because a result names the call alone -- so a
    call that ran several commands (a compound shell line, a codex program) loses every skill it
    named as soon as any part of it fails, and a required skill really invoked there reads as never
    invoked. Erring the other way is worse: it is the broken-workspace reading above.
    """
    errored_call_ids = _errored_call_ids(steps)
    invocations: list[SkillInvocation] = []
    for step in steps:
        if step.get("source") != "agent":
            continue
        for tool_call in step.get("tool_calls") or []:
            if not isinstance(tool_call, Mapping):
                continue
            if str(tool_call.get("tool_call_id") or "") in errored_call_ids:
                continue
            invocations.extend(
                SkillInvocation(
                    name=name,
                    tool_call_id=str(tool_call.get("tool_call_id") or ""),
                    step_id=str(step.get("step_id") or ""),
                )
                for name in _skills_invoked_by_call(tool_call)
            )
    return tuple(invocations)


@pure
def build_worker_trajectory_from_stream(stream_content: str, agent_id: str, agent_type: str) -> dict[str, Any]:
    """A destroyed worker's document, built host-side from its preserved stream with mngr's own builder,
    enriched the way `mngr transcript --format atif` would have enriched it in the workspace.

    Raises TrajectoryDocumentError when the stream cannot produce a valid document.
    """
    enrichment = TrajectoryEnrichment(
        agent_name=agent_type, agent_version="unknown", session_id=agent_id, trajectory_id=agent_id
    )
    try:
        records = parse_stream_content(stream_content, "worker {} stream".format(agent_id))
        build_result = build_trajectory_from_records(records, enrichment, {})
    except TrajectoryBuildError as exc:
        raise TrajectoryDocumentError("the worker's stream cannot be built into a trajectory: {}".format(exc)) from exc
    return build_result.trajectory.to_json_dict()


@pure
def _with_worker_ref(step: Mapping[str, Any], tool_call_id: str, ref: Mapping[str, Any]) -> dict[str, Any]:
    """The step with the ref attached to the launching call's observation result, or to a synthesized
    pending result when the launch's own output never arrived (the same placeholder mngr's builder uses)."""
    observation = step.get("observation")
    results = list(observation.get("results") or []) if isinstance(observation, Mapping) else []
    for index, result in enumerate(results):
        if isinstance(result, Mapping) and result.get("source_call_id") == tool_call_id:
            results[index] = {
                **result,
                "subagent_trajectory_ref": [*(result.get("subagent_trajectory_ref") or []), ref],
            }
            break
    else:
        results.append(
            {
                "source_call_id": tool_call_id,
                "content": None,
                "subagent_trajectory_ref": [ref],
                "extra": {"subagent_result_pending": True},
            }
        )
    return {**step, "observation": {**(observation if isinstance(observation, Mapping) else {}), "results": results}}


@pure
def graft_worker_trajectories(document: Mapping[str, Any], workers: Sequence[EmbeddedWorker]) -> dict[str, Any]:
    """The document with each worker embedded under the call that launched it, in ATIF v1.7's form: a
    `subagent_trajectory_ref` on the launching call's observation result and the worker's trajectory
    in `subagent_trajectories`, stamped `subagent_kind: "mngr"` like mngr's own embedded siblings.

    A worker whose launching call is not in this document is still embedded, without a ref, so the
    delegated work is never dropped over a mismatch.
    """
    steps = [dict(step) for step in document.get("steps") or []]
    embedded = list(document.get("subagent_trajectories") or [])
    for worker in workers:
        worker_id = str(worker.document.get("trajectory_id") or "")
        ref = {
            "trajectory_id": worker_id,
            "extra": {"subagent_kind": MNGR_SUBAGENT_KIND, "worker_name": worker.launch.name},
        }
        for index, step in enumerate(steps):
            if any(
                isinstance(tool_call, Mapping) and tool_call.get("tool_call_id") == worker.launch.tool_call_id
                for tool_call in step.get("tool_calls") or []
            ):
                steps[index] = _with_worker_ref(step, worker.launch.tool_call_id, ref)
                break
        embedded.append(
            {
                **worker.document,
                "extra": {
                    **(worker.document.get("extra") or {}),
                    "subagent_kind": MNGR_SUBAGENT_KIND,
                    "worker": {
                        "name": worker.launch.name,
                        "agent_id": worker.agent_id,
                        "state": worker.state.value,
                        "lead_agent_id": document.get("session_id"),
                        "launch_tool_call_id": worker.launch.tool_call_id,
                        "report_path": worker.report_path,
                    },
                },
            }
        )
    return {**document, "steps": steps, "subagent_trajectories": embedded if embedded else None}


@pure
def build_hand_built_trajectory(
    conversation: Sequence[Mapping[str, str]],
    provenance: TrajectoryProvenance,
    workspace_usage: TrialUsage,
    timestamp: str,
    boundaries: Sequence[StepBoundary],
) -> Trajectory | None:
    """The fallback trajectory when the workspace could not provide its document: one step per clean
    conversation turn, carrying the same reconciled fields. None when there was no exchange at all,
    since ATIF requires at least one step."""
    conversation_step_count = sum(1 for entry in conversation if entry["text"].strip())
    if not conversation_step_count:
        return None
    # The conversation's own turns, not the harness's markers.
    final_metrics = _resolved_final_metrics(workspace_usage, conversation_step_count)
    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=provenance.harbor_session_id,
        agent=TrajectoryAgent(name=provenance.driver_name, version=provenance.driver_version),
        steps=conversation_steps_with_boundaries(conversation, boundaries, timestamp),
        # total_steps counts conversation turns, not LLM calls, on this shape.
        final_metrics=final_metrics,
        extra=_provenance_extra(provenance, TrajectorySource.HAND_BUILT),
    )
