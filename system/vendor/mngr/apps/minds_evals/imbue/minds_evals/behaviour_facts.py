"""The behaviour family's check-time facts: what one probe step's records say the agent did, whether
the two records that say it could be read, and whether every other record of the trial agrees.

Three kinds, and the table tells them apart:

- **compliance** (`agent.*`) says whether the agent did what the prompt asked, read only from the
  diagnostic probe and the event feed, whose parsers (`diagnostic_probe.py`) share nothing with the
  collectors and renderers under test. A regression in the tickets capture, the agent listing, the
  captured trajectory or the verifier's renderers therefore cannot read as "the agent did not
  comply".
- **health** (`probe.read`, `feed.inputs_read`) says whether those two sources answered at all.
- **instrument** (everything else here) is an agreement between records that were written
  independently: the tickets capture, the captured trajectory, the event feed, the verifier's
  derived outputs, the worker captures, and the manifest's process entries, which hold what the
  collector's readers made of the captured stream.

The whole block is due only on a step whose case config sets `diagnostic_probe`, which is what makes
the collector take the probe and the driver read the feed's tool inputs. Every other step omits it.
"""

import json
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.minds_evals import diagnostic_probe
from imbue.minds_evals import evidence_collection
from imbue.minds_evals.check_run import harness_config_block
from imbue.minds_evals.data_types import DiagnosticProbeReading
from imbue.minds_evals.data_types import FactValue
from imbue.minds_evals.data_types import NOT_RECORDED
from imbue.minds_evals.data_types import TicketRecord
from imbue.minds_evals.driver import DriverEventType
from imbue.minds_evals.driver import is_model_confirmed
from imbue.minds_evals.driver import parse_harness_config
from imbue.minds_evals.errors import AgentKwargError
from imbue.minds_evals.fact_sources import ReadFile
from imbue.minds_evals.fact_sources import StepFactSources
from imbue.minds_evals.trajectory import scan_skill_invocations
from imbue.minds_evals.trajectory import scan_worker_launches

# The tools whose results carry command output, which is what the verifier's judged transcript and
# its failure scan read. It must equal the `EXECUTING_TOOLS` of both verifier renderers
# (templates/tests/verifier/render_judge_transcript.py and render_harness_report.py), and
# `behaviour_facts_test.py` pins it against them. It is a constant rather than a lazy read of those
# templates because a fact function must stay a pure reading of the step's records, and because
# executing a verifier template is not something the checker should do per trial.
EXECUTING_TOOLS: Final[frozenset[str]] = frozenset(
    {"Bash", "BashOutput", "bash", "shell", "shell_command", "exec_command", "write_stdin", "exec", "wait"}
)

# How render_judge_transcript.py opens a rendered progress block, and the two kinds it renders.
PROGRESS_BLOCK_PREFIX: Final[str] = "[PROGRESS · "
_DECLARED_BLOCK_HEADER: Final[str] = "[PROGRESS · step declared]"
_DONE_BLOCK_HEADER: Final[str] = "[PROGRESS · step done]"

# The signature render_harness_report.py counts a command that does not exist under.
MISSING_COMMAND_SIGNATURE: Final[str] = "missing_command"

# The step tickets the behaviour prompt asks for, matched by title because a harness's own reminders
# create step tickets the prompt did not ask for.
_WANTED_STEP_TITLES: Final[frozenset[str]] = frozenset(diagnostic_probe.STEP_TICKET_TITLES)

_FACT_NAMES_FROM_PROBE: Final[tuple[str, ...]] = (
    "probe.read",
    "agent.step_tickets",
    "agent.regular_ticket",
    "agent.worker_launched",
    "agent.worker_finished",
    "steps.upload_marker_present",
)
_FACT_NAMES_FROM_FEED: Final[tuple[str, ...]] = (
    "feed.tool_call_count",
    "feed.tool_calls_without_input",
    "feed.inputs_read",
    "agent.ran_nonce_echo",
    "agent.ran_missing_command",
    "agent.read_upload_marker",
    "agent.invoked_skill",
)
_FACT_NAMES_FROM_TRAJECTORY: Final[tuple[str, ...]] = (
    "tools.nonce_in_one_executing_result",
    "failures.missing_command_is_error",
    "steps.upload_marker_read",
    "workers.model_is_lead_model",
    "process.invoked_skills",
    "process.worker_launch_count",
)


@pure
def _not_recorded(fact_names: Sequence[str]) -> dict[str, FactValue]:
    return {fact_name: NOT_RECORDED for fact_name in fact_names}


@pure
def _null(fact_names: Sequence[str]) -> dict[str, FactValue]:
    return dict.fromkeys(fact_names, None)


@pure
def is_behaviour_block_due(sources: StepFactSources) -> bool:
    """Whether the step's own case config asked for the probe, which is what makes its two compliance
    records exist. A case that asks for neither has no behaviour facts to state."""
    step = sources.case.step if sources.case is not None else None
    return step is not None and step.is_diagnostic_probe_run


# --- the diagnostic probe (D) ---


@pure
def read_diagnostic_probe(capture: ReadFile) -> DiagnosticProbeReading | None:
    """The probe's reading, or None when the file says it did not answer or does not parse.

    A capture that reports a failure carries `failure_reason: <why>` on its first line, so an unrun
    probe is never read as a workspace that holds nothing.
    """
    if not capture.is_readable:
        return None
    if capture.text.startswith(evidence_collection.DIAGNOSTIC_PROBE_FAILURE_PREFIX):
        return None
    return diagnostic_probe.parse_diagnostic_probe(capture.text)


@pure
def _probe_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """What the probe saw of the tickets, the agents, the worker's report and the step's upload."""
    if not sources.diagnostic_probe.is_present:
        return _not_recorded(_FACT_NAMES_FROM_PROBE)
    reading = read_diagnostic_probe(sources.diagnostic_probe)
    if reading is None:
        return {**_null(_FACT_NAMES_FROM_PROBE), "probe.read": False}
    step_titles = {ticket.title for ticket in reading.tickets if ticket.is_step and ticket.status == "closed"}
    regular_titles = {ticket.title for ticket in reading.tickets if not ticket.is_step}
    return {
        "probe.read": True,
        "agent.step_tickets": set(diagnostic_probe.STEP_TICKET_TITLES) <= step_titles,
        "agent.regular_ticket": diagnostic_probe.REGULAR_TICKET_TITLE in regular_titles,
        # None rather than False wherever the probe's own `mngr list` could not be read: an agent
        # listing that did not answer says nothing about whether the worker was launched.
        "agent.worker_launched": None
        if reading.agents is None
        else any(agent.name == diagnostic_probe.WORKER_NAME for agent in reading.agents),
        "agent.worker_finished": diagnostic_probe.WORKER_REPORT_PATH in reading.report_paths,
        "steps.upload_marker_present": diagnostic_probe.UPLOAD_MARKER_PATH in reading.upload_paths,
    }


# --- the event feed (E) ---


@pure
def feed_events(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The workspace events the driver polled, out of its own `driver_events.jsonl` records."""
    return [
        event
        for record in records
        if record.get("type") == DriverEventType.FEED_EVENT.value
        for event in [record.get("event")]
        if isinstance(event, Mapping)
    ]


@pure
def feed_detail_by_event_id(records: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any] | None]:
    """Each event's detail payload, which is null for an event the chat app did not serve one for."""
    return {
        str(record.get("event_id") or ""): record.get("detail") if isinstance(record.get("detail"), Mapping) else None
        for record in records
        if record.get("type") == DriverEventType.EVENT_DETAIL.value
    }


@pure
def _feed_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """Whether every tool call the feed shows had its input read, and what those inputs say the agent
    ran. The inputs are the feed's own record of the commands, independent of the captured
    trajectory the verifier's readers work from."""
    if not sources.is_driver_events_present:
        return _not_recorded(_FACT_NAMES_FROM_FEED)
    events = feed_events(sources.driver_events)
    call_ids = list(dict.fromkeys(call_id for _event_id, call_id in diagnostic_probe.feed_tool_calls(events)))
    input_by_call_id = diagnostic_probe.feed_tool_input_by_call_id(feed_detail_by_event_id(sources.driver_events))
    tool_name_by_call_id = diagnostic_probe.feed_tool_name_by_call_id(events)
    unread_call_ids = [call_id for call_id in call_ids if call_id not in input_by_call_id]
    read_call_ids = [call_id for call_id in call_ids if call_id in input_by_call_id]
    inputs = [input_by_call_id[call_id] for call_id in read_call_ids]
    return {
        "feed.tool_call_count": len(call_ids),
        "feed.tool_calls_without_input": len(unread_call_ids),
        "feed.inputs_read": not unread_call_ids,
        "agent.ran_nonce_echo": any(diagnostic_probe.NONCE_ECHO_COMMAND in text for text in inputs),
        "agent.ran_missing_command": any(diagnostic_probe.MISSING_COMMAND in text for text in inputs),
        "agent.read_upload_marker": any(diagnostic_probe.UPLOAD_MARKER_PATH in text for text in inputs),
        "agent.invoked_skill": any(
            diagnostic_probe.is_skill_invocation(tool_name_by_call_id.get(call_id, ""), input_by_call_id[call_id])
            for call_id in read_call_ids
        ),
    }


# --- the captured trajectory (A) ---


@pure
def _agent_steps(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    steps = document.get("steps")
    return [step for step in steps if isinstance(step, Mapping)] if isinstance(steps, list) else []


@pure
def _tool_calls(step: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    calls = step.get("tool_calls")
    return [call for call in calls if isinstance(call, Mapping)] if isinstance(calls, list) else []


@pure
def _results(step: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    observation = step.get("observation")
    results = observation.get("results") if isinstance(observation, Mapping) else None
    return [result for result in results if isinstance(result, Mapping)] if isinstance(results, list) else []


@pure
def _tool_name_by_call_id(document: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(call.get("tool_call_id") or ""): str(call.get("function_name") or "")
        for step in _agent_steps(document)
        for call in _tool_calls(step)
    }


@pure
def executing_tool_result_texts(document: Mapping[str, Any]) -> list[str]:
    """Every tool result the verifier's renderers read as command output, which is the result of a
    call whose tool is one of `EXECUTING_TOOLS`, in trajectory order."""
    tool_by_call_id = _tool_name_by_call_id(document)
    return [
        str(result.get("content") or "")
        for step in _agent_steps(document)
        for result in _results(step)
        if tool_by_call_id.get(str(result.get("source_call_id") or "")) in EXECUTING_TOOLS
    ]


@pure
def tool_result_texts(document: Mapping[str, Any]) -> list[str]:
    """Every tool result's text, whatever tool produced it, in trajectory order."""
    return [str(result.get("content") or "") for step in _agent_steps(document) for result in _results(step)]


@pure
def is_result_of_command_errored(document: Mapping[str, Any], command: str) -> bool | None:
    """Whether the trajectory marks the result of the call that ran `command` as an error.

    None when no call's arguments name the command or no result answers one that does: the readers
    downstream mark errors, and a result that is not there cannot say whether they did. The whole
    argument block is searched rather than one key, since a harness in code mode carries the command
    inside the program it wrote (`_raw`) rather than under `command`.
    """
    call_ids = {
        str(call.get("tool_call_id") or "")
        for step in _agent_steps(document)
        for call in _tool_calls(step)
        if command in json.dumps(call.get("arguments"))
    }
    errored = [
        _is_result_errored(result)
        for step in _agent_steps(document)
        for result in _results(step)
        if str(result.get("source_call_id") or "") in call_ids
    ]
    return any(errored) if errored else None


@pure
def _is_result_errored(result: Mapping[str, Any]) -> bool:
    extra = result.get("extra")
    return isinstance(extra, Mapping) and extra.get("is_error") is True


@pure
def _worker_trajectory(document: Mapping[str, Any], worker_name: str) -> Mapping[str, Any] | None:
    """The named worker's own captured document, as the published trajectory embeds it."""
    subagents = document.get("subagent_trajectories")
    for subagent in subagents if isinstance(subagents, list) else []:
        extra = subagent.get("extra") if isinstance(subagent, Mapping) else None
        worker = extra.get("worker") if isinstance(extra, Mapping) else None
        if isinstance(worker, Mapping) and str(worker.get("name") or "") == worker_name:
            return subagent
    return None


@pure
def is_worker_on_the_lead_model(sources: StepFactSources, document: Mapping[str, Any]) -> bool | None:
    """Whether the captured worker's steps name the model the lead was switched to.

    Read through the driver's own confirmation, so a catalog id and the name it reports as are
    compared the one way the trial compares them. None wherever nothing can be told: no worker was
    captured, its steps carry no model name at all (which is every codex trial), or the arm requested
    no switch.
    """
    worker = _worker_trajectory(document, diagnostic_probe.WORKER_NAME)
    if worker is None:
        return None
    observed = sorted(
        {
            str(step.get("model_name"))
            for step in _agent_steps(worker)
            if step.get("source") == "agent" and step.get("model_name")
        }
    )
    arm = harness_config_block(sources.state)
    try:
        harness_config = parse_harness_config(
            lane=arm.get("lane") or "",
            key_provider=arm.get("key_provider") or "",
            key_env="",
            model=arm.get("model") or "",
            effort=arm.get("effort") or "",
            fast="true" if arm.get("fast") is True else "",
        )
    except AgentKwargError:
        return None
    return is_model_confirmed(harness_config, observed)


@pure
def _trajectory_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """The agreements the captured trajectory answers on its own: the nonce echo's output, whether
    the failing command's result is marked as an error, the upload's text, the worker's model, and
    what the process readers make of the document.

    The `process.*` pair runs the readers the collector's process checks run, over the captured
    document rather than over the stream the collector read, so what the manifest's process entries
    record has a second record to agree with."""
    if sources.trajectory is None:
        return _not_recorded(_FACT_NAMES_FROM_TRAJECTORY)
    document = sources.trajectory
    steps = _agent_steps(document)
    result_texts = executing_tool_result_texts(document)
    nonce_result_count = sum(diagnostic_probe.NONCE_ECHO_OUTPUT in text for text in result_texts)
    return {
        "tools.nonce_in_one_executing_result": nonce_result_count == 1,
        "failures.missing_command_is_error": is_result_of_command_errored(document, diagnostic_probe.MISSING_COMMAND),
        # Read over every tool result rather than the executing ones: the prompt asks the agent to
        # read the upload's marker, not to run a command, and a harness whose file-reading tool
        # answers it (pi's `read`) puts the text in a result no executing tool produced.
        "steps.upload_marker_read": any(
            diagnostic_probe.UPLOAD_MARKER_TEXT in text for text in tool_result_texts(document)
        ),
        "workers.model_is_lead_model": is_worker_on_the_lead_model(sources, document),
        "process.invoked_skills": sorted({invocation.name for invocation in scan_skill_invocations(steps)}),
        "process.worker_launch_count": len(scan_worker_launches(steps, depth=0, lead_name="")),
    }


# --- the verifier's derived outputs (R) ---


@pure
def _json_object(read: ReadFile) -> dict[str, Any] | None:
    if not read.is_readable:
        return None
    try:
        parsed = json.loads(read.text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


@pure
def progress_blocks(transcript: str) -> list[tuple[str, str]]:
    """Every rendered progress block of the judged transcript, as (its header, its body).

    `render_judge_transcript.py` joins blocks with a blank line and opens each progress block with
    its own header line, so the body is whatever the block carries under that: the declared title,
    or the carried-forward title and then the close summary.
    """
    return [
        (block.split("\n", 1)[0], block.split("\n", 1)[1] if "\n" in block else "")
        for block in transcript.split("\n\n")
        if block.startswith(PROGRESS_BLOCK_PREFIX)
    ]


@pure
def _declared_titles(transcript: str) -> list[str]:
    return [body.strip() for header, body in progress_blocks(transcript) if header == _DECLARED_BLOCK_HEADER]


@pure
def _done_summaries(transcript: str) -> list[str]:
    """Each close block's summary, which is its last line: a block whose step's title was seen
    carries that title above the summary, and one whose was not carries the summary alone."""
    return [
        body.strip().rsplit("\n", 1)[-1].strip()
        for header, body in progress_blocks(transcript)
        if header == _DONE_BLOCK_HEADER
    ]


@pure
def _diagnostic_step_tickets(tickets: Sequence[TicketRecord]) -> list[TicketRecord]:
    return [record for record in tickets if record.is_step and record.title in _WANTED_STEP_TITLES]


@pure
def _appears_exactly_once(wanted: Sequence[str], rendered: Sequence[str]) -> bool:
    """Whether every wanted text is one of the rendered ones exactly once. Empty wanted texts are no
    agreement at all, so they read as a miss rather than as a vacuous pass."""
    return bool(wanted) and all(sum(text == candidate for candidate in rendered) == 1 for text in wanted)


@pure
def _progress_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """Whether the tickets, the trajectory, the feed and the rendered timeline tell the same story
    about the step tickets the prompt asked for."""
    facts: dict[str, FactValue] = {}
    if not sources.is_tickets_present or sources.trajectory is None or not sources.is_driver_events_present:
        facts["progress.step_ids_agree"] = NOT_RECORDED
    elif sources.tickets_failure_reason:
        facts["progress.step_ids_agree"] = None
    else:
        ticket_ids = sorted(record.ticket_id for record in _diagnostic_step_tickets(sources.tickets))
        trajectory_ids = diagnostic_probe.step_ids_by_title(
            executing_tool_result_texts(sources.trajectory), diagnostic_probe.STEP_TICKET_TITLES
        )
        feed_ids = diagnostic_probe.step_ids_by_title(
            diagnostic_probe.feed_tk_stamps(feed_events(sources.driver_events)), diagnostic_probe.STEP_TICKET_TITLES
        )
        facts["progress.step_ids_agree"] = bool(ticket_ids) and ticket_ids == trajectory_ids == feed_ids
    facts.update(_rendered_progress_facts(sources))
    return facts


@pure
def _rendered_progress_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """What the judged transcript's progress timeline made of the same tickets."""
    fact_names = (
        "progress.declared_titles_agree",
        "progress.done_summaries_agree",
        "progress.nonce_block_count",
        "progress.regular_ticket_rendered",
        "progress.rendered_block_count",
    )
    if not sources.judge_transcript.is_present or not sources.progress_summary.is_present:
        return _not_recorded(fact_names)
    if not sources.judge_transcript.is_readable:
        return _null(fact_names)
    transcript = sources.judge_transcript.text
    summary = _json_object(sources.progress_summary)
    blocks = progress_blocks(transcript)
    step_tickets = _diagnostic_step_tickets(sources.tickets) if sources.is_tickets_present else ()
    return {
        "progress.declared_titles_agree": _appears_exactly_once(
            sorted(record.title for record in step_tickets), _declared_titles(transcript)
        ),
        "progress.done_summaries_agree": _appears_exactly_once(
            sorted(record.summary for record in step_tickets if record.status == "closed"),
            _done_summaries(transcript),
        ),
        "progress.nonce_block_count": sum(diagnostic_probe.NONCE in body for _header, body in blocks),
        "progress.regular_ticket_rendered": any(
            diagnostic_probe.REGULAR_TICKET_TITLE in body for _header, body in blocks
        ),
        # The verifier's own count of what it rendered, beside the blocks read back out of the text
        # it rendered them into: the two disagreeing is the renderer and its summary drifting apart.
        "progress.rendered_block_count": None if summary is None else _counted(summary.get("rendered_block_count")),
    }


@pure
def _counted(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@pure
def _failure_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """How many steps of the lead's own trajectory the verifier's failure scan counted a missing
    command in. Its counts are the numbers the harness-quality score is computed from."""
    if not sources.harness_failures.is_present:
        return _not_recorded(("failures.missing_command_count",))
    recorded = _json_object(sources.harness_failures)
    main = recorded.get("main") if isinstance(recorded, dict) else None
    counts = main.get("counts") if isinstance(main, Mapping) else None
    if not isinstance(counts, Mapping):
        return {"failures.missing_command_count": None}
    return {"failures.missing_command_count": _counted(counts.get(MISSING_COMMAND_SIGNATURE)) or 0}


# --- the worker captures (W) ---


@pure
def _worker_capture_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """Whether the worker's finish report -- the deliverable the prompt asked it for -- came out of
    the capture, beside the probe's reading that the workspace holds it."""
    if not sources.is_worker_captures_present:
        return _not_recorded(("workers.report_captured",))
    captured = [capture for capture in sources.worker_captures if capture.name == diagnostic_probe.WORKER_NAME]
    return {"workers.report_captured": any(capture.is_report_captured for capture in captured) if captured else None}


# --- the block ---


@pure
def compute_behaviour_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """One probe step's behaviour block, or an empty one on a step whose case asked for no probe."""
    if not is_behaviour_block_due(sources):
        return {}
    return {
        **_probe_facts(sources),
        **_feed_facts(sources),
        **_trajectory_facts(sources),
        **_progress_facts(sources),
        **_failure_facts(sources),
        **_worker_capture_facts(sources),
    }
