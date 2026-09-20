"""The self-diagnostic facts: named readings of what one step of a trial recorded.

`compute_step_facts` turns one step's persisted records -- as `fact_sources.py` read them off the job
directory -- into a flat mapping from a dotted fact name to a value. Every fact is computed at check
time, from the same files the graders read, so a fact can only be right if the record is right.
Nothing in the driver computes or stores one, no verifier reads one, and nothing here feeds a reward.

A fact reports one of three things, and a table tells them apart:

- **omitted** (absent from the block): the record was never due on this step. The case declared no
  such check, the trial pulled no snapshot, the step is the first so there is no earlier one to be
  step-local with respect to. The case config in the step's `instruction.md` is what says what was
  declared.
- **not recorded** (`NOT_RECORDED`): the record was due on a step that ran and is absent or
  unreadable. That is the instrument failing to write its record, whatever the table expected, and
  it is never the agent's doing.
- **null**: the record is there and cannot answer -- a registry the collector could not resolve, a
  ticket capture that reports a failure, a listing too incomplete to speak for an absent worker.

`manifest.readable` is the one reading that is never "not recorded": it exists precisely to say
whether the manifest was there and well-formed, so that the healthy value a schemaless reader maps a
missing file onto is never reached through the gap.

The behaviour family's own facts -- the diagnostic probe, the event feed's tool-call inputs, and the
agreements between the rendered progress timeline and the ticket records -- live in
`behaviour_facts.py` and are folded into the block here. They are due only on a step whose case
config asks for the probe, and every other step omits them.
"""

import re
from collections import Counter
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Final
from urllib.parse import urlsplit

from pydantic import Field
from pydantic import JsonValue
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals import behaviour_facts
from imbue.minds_evals import evidence_collection
from imbue.minds_evals import trajectory as trajectory_reading
from imbue.minds_evals import ui_flows
from imbue.minds_evals.check_run import GATES_DIMENSION
from imbue.minds_evals.check_run import collect_error_entry_ids
from imbue.minds_evals.check_run import criteria_of_dimension
from imbue.minds_evals.check_run import criterion_value
from imbue.minds_evals.check_run import harness_config_block
from imbue.minds_evals.check_run import is_gates_dimension_passed
from imbue.minds_evals.data_types import CheckClass
from imbue.minds_evals.data_types import EntryRecord
from imbue.minds_evals.data_types import ExpandedExpectations
from imbue.minds_evals.data_types import FactValue
from imbue.minds_evals.data_types import GoalSatisfactionTiming
from imbue.minds_evals.data_types import ManifestEntry
from imbue.minds_evals.data_types import NOT_RECORDED
from imbue.minds_evals.data_types import PreparationStage
from imbue.minds_evals.data_types import RegisteredApp
from imbue.minds_evals.data_types import TurnRecord
from imbue.minds_evals.data_types import WorkerListingEntry
from imbue.minds_evals.data_types import WorkerState
from imbue.minds_evals.expectations import slugify
from imbue.minds_evals.fact_sources import CapturedWorkerRecord
from imbue.minds_evals.fact_sources import CheckTimeReadings
from imbue.minds_evals.fact_sources import FlowSources
from imbue.minds_evals.fact_sources import StepFactSources
from imbue.minds_evals.fact_sources import driver_log_timestamps
from imbue.minds_evals.fact_sources import registry_rows
from imbue.minds_evals.resources.flow_step_protocol import StepReaction

# How much of a trajectory's agent steps name the model that answered them.
MODEL_NAME_COVERAGE_ALL: Final[str] = "all"
MODEL_NAME_COVERAGE_NONE: Final[str] = "none"
MODEL_NAME_COVERAGE_SOME: Final[str] = "some"

# `flow.<slug>.step.<k>.observed_kind`: which `ui_flows` summary constant a record's `observed` is,
# named rather than quoted so a reworded constant does not change the fact.
OBSERVED_KIND_CHANGED: Final[str] = "changed"
OBSERVED_KIND_UNCHANGED: Final[str] = "unchanged"
OBSERVED_KIND_NO_REACTION: Final[str] = "no_reaction"
OBSERVED_KIND_ACKNOWLEDGED_ONLY: Final[str] = "acknowledged_only"
OBSERVED_KIND_STILL_CHANGING_UNCHANGED: Final[str] = "still_changing_unchanged"

# The facts every step owes, whatever else it recorded: they are read from `state.json`, which the
# driver writes on every turn of every step, so a step that ran and has none recorded nothing.
_STATE_FACT_NAMES: Final[tuple[str, ...]] = (
    "prep.stage_reached",
    "arm.harness_config.harness",
    "arm.harness_config.model_choice_switch",
    "arm.harness_config.is_model_confirmed",
    "entries.count",
    "decider.call_count",
)
# The facts read off the published trajectory, once a step has one to read.
_TRAJECTORY_FACT_NAMES: Final[tuple[str, ...]] = (
    "transcript.source",
    "transcript.user_step_count",
    "transcript.agent_steps_with_model_name",
    "tools.every_call_has_one_result",
    "workers.embedded",
    "steps.boundary_markers",
    "steps.boundary_markers_match_case",
)
# What the per-step spend and worker counts are read from, inside harbor's step metadata.
_TRANSCRIPT_USAGE_KEY: Final[str] = "transcript_usage"
_WORKSPACE_USAGE_KEY: Final[str] = "workspace_usage"
_USAGE_FACT_NAMES: Final[tuple[str, ...]] = (
    "usage.tokens_present",
    "usage.is_cost_complete",
    "workers.launch_count",
    "workers.captured_count",
)

# The state keys the driver's own writes carry, spelled here rather than imported from `driver.py`,
# which would pull harbor's agent base into the CLI's graph. A test holds `STATE_KEYS_READ` against
# the driver's payload, because drift here is silent: a renamed key reads as a trial that recorded
# nothing rather than as a checker that looked in the wrong place.
_PREPARATION_STAGE_KEY: Final[str] = "preparation_stage"
_ENTRIES_KEY: Final[str] = "entries"
_DECIDER_CALL_COUNT_KEY: Final[str] = "decider_call_count"
_CLIENT_MESSAGES_KEY: Final[str] = "client_messages"
_SNAPSHOT_BYTE_COUNT_KEY: Final[str] = "snapshot_byte_count"
# The seed record a seeded trial's state carries, written by the driver's seed build; a case that
# seeds nothing leaves it null and its `seed.*` facts omitted. It is read here because the verdict
# turns on it: a seed that could not apply is the suite's own failure and must not read as a night
# that was never measured.
_SEED_KEY: Final[str] = "seed"
_SEED_BUILD_STATUS_KEY: Final[str] = "build_status"
# The conversation records the timing facts read: one per answered client message, and the trial's
# own wall-clock, which is the span the measured one has to sit inside.
_TURNS_KEY: Final[str] = "turns"
_ELAPSED_SECONDS_KEY: Final[str] = "elapsed_seconds"
STATE_KEYS_READ: Final[tuple[str, ...]] = (
    _PREPARATION_STAGE_KEY,
    _ENTRIES_KEY,
    _DECIDER_CALL_COUNT_KEY,
    _CLIENT_MESSAGES_KEY,
    _SNAPSHOT_BYTE_COUNT_KEY,
    _TURNS_KEY,
    _ELAPSED_SECONDS_KEY,
)

# The facts a case that declares a `timing` block owes; a case that declares none owes no reading.
_TIMING_FACT_NAMES: Final[tuple[str, ...]] = (
    "timing.turn_index",
    "timing.seconds_within_elapsed",
    "timing.agrees_with_feed",
)
# What `driver_events.jsonl` calls a record carrying one workspace event, spelled here for the reason
# the state keys above are: importing the driver's enum would pull harbor's agent base into the CLI's
# graph, and a renamed kind must be caught by the test that holds this against the driver rather than
# read as a trial whose feed recorded nothing.
FEED_EVENT_RECORD_TYPE: Final[str] = "feed_event"
# How far apart the driver's clock and the workspace's stamp of one event may stand and still be
# read as that one event. Neither end of the span is stamped at the same instant in both records: the
# driver takes its anchor once the send round trip has come back, and its close once a poll has seen
# the agent idle again, so each of its timestamps trails the workspace's by a bridge round trip or
# more. This holds those round trips with room to spare and stays far below the disagreement worth
# catching, which is a span anchored on the wrong message -- the welcome turn and the trial's start
# are minutes from the first client message, not seconds.
FEED_ROUND_TRIP_SECONDS: Final[float] = 30.0

# Every preparation stage is reached only once the workspace exists, and the evidence phase runs at
# the end of every step that had one, so a stage from here on is what makes a collection due -- and,
# for the checker, what tells a trial that produced a workspace from one that never did.
WORKSPACE_CREATED_STAGES: Final[frozenset[str]] = frozenset(stage.value for stage in PreparationStage)

# What every workspace snapshot tars its tree under, so a tarball listing without it is not one.
# `minds_bridge` writes it with `tar -C <home> .`, which stores each member under `./`, and adds the
# preserved and minds directories as their own `-C /` segments, so a member's leading `.` is dropped
# before the tree is looked for.
_SNAPSHOT_ROOT_MEMBER: Final[str] = "workspace"

# `repo_state.json`'s own key names. The collector writes the first two today; the split between the
# workspace's own first-boot commit and the agent's is what a seeded trial needs to tell them apart,
# and until it is written both counts read null rather than crediting one to the other.
_REPO_SEEDED_IN_HEAD_KEY: Final[str] = "is_seeded_sha_in_head"
_REPO_COMMIT_COUNT_KEY: Final[str] = "commit_count_beyond_base"
_REPO_AGENT_COMMIT_COUNT_KEY: Final[str] = "agent_commit_count"
_REPO_BOOTSTRAP_COMMIT_COUNT_KEY: Final[str] = "bootstrap_commit_count"

# The manifest key the collector records the seeded registrations under. Read off the raw object
# rather than the model, so a job directory written before the key existed reads as "did not say".
_SEEDED_REGISTRATIONS_KEY: Final[str] = "seeded_registrations"

# What supervisord reports for a program that is up.
_RUNNING_SERVICE_STATE: Final[str] = "RUNNING"

# Where `finalize.py` writes the harness it detected in reward-details.json.
_HARNESS_BLOCK_KEY: Final[str] = "harness"

# The line render_flow_evidence.py opens each flow's detail with in the judge's digest.
_DIGEST_FLOW_HEADING_PREFIX: Final[str] = "## flow: "

# What render_flow_evidence.py writes into the digest for a step performed by ref, and the note it
# adds to the index when any step was. Spelled here rather than imported: the renderer is a stdlib
# script that runs in the verifier container, and a drift test holds these against its source.
_DIGEST_REF_STEP_PREFIX: Final[str] = "addressed by ref:"
_DIGEST_UNNAMED_CONTROL_NOTE_PREFIX: Final[str] = "Note on unnamed controls:"
DIGEST_MARKERS_READ: Final[tuple[str, ...]] = (
    _DIGEST_FLOW_HEADING_PREFIX,
    _DIGEST_REF_STEP_PREFIX,
    _DIGEST_UNNAMED_CONTROL_NOTE_PREFIX,
)

# What the closing `done` decision, and a decision the executor could not carry out, are recorded as.
# Neither performs anything, so neither is one of the flow's actions for the reaction tally: folding
# an agent that could not decide into the no-reaction share would read as the app not answering.
_DONE_ACTION_TEXT: Final[str] = ui_flows.describe_action(
    ui_flows.FlowAction(
        kind=ui_flows.FlowActionKind.DONE, role="", target="", ref="", text="", amount=0, reasoning="", expected=""
    )
)
_NO_REACTION_VALUE: Final[str] = StepReaction.NONE.value
# How many places `no_reaction_share` keeps, so the recorded value reads as a share.
_SHARE_DIGITS: Final[int] = 4

# One ARIA snapshot line, once `ui_flows.comparable_line` has dropped the recorder's per-line
# suffixes: `- checkbox ["<name>"] [checked]`. A checkbox the page gives no accessible name prints no
# name at all, and reads as the empty name.
_CHECKBOX_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r'^- checkbox(?: "(?P<name>(?:\\.|[^"\\])*)")?(?P<rest>(?:[ :].*)?)$'
)
_CHECKED_ATTRIBUTE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\[checked\]")
_ESCAPE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\\(.)")
# The first line of every recorded page state: `page <url> (<title>)`.
_PAGE_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^page (?P<url>\S+) \(")


@pure
def _optional_int(value: Any) -> int | None:
    """The value as an int, or None for anything else. A boolean is excluded, since `isinstance(True,
    int)` holds and a flag is never a count."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@pure
def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


@pure
def _optional_float(value: Any) -> float | None:
    """The value as a float, or None for anything else. A boolean is excluded for the reason it is
    in `_optional_int`: a flag is never a measurement."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


@pure
def _counted(value: Any) -> int | None:
    """A count recorded either as a number or as the digits a shell probe printed."""
    if isinstance(value, str):
        return int(value) if value.strip().isdigit() else None
    return _optional_int(value)


@pure
def _not_recorded(fact_names: Sequence[str]) -> dict[str, FactValue]:
    return {fact_name: NOT_RECORDED for fact_name in fact_names}


# --- what the step's own state recorded ---


@pure
def _state_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """`prep.*`, `arm.*`, `entries.*`, `decider.*`, `seed.build_status` and `snapshot.bytes`.

    The seed and snapshot facts are omitted where the state records neither: a case that seeds
    nothing has no build to report, and a trial that pulled no snapshot has no tarball to read.
    """
    state = sources.state
    if state is None:
        return _not_recorded(_STATE_FACT_NAMES)
    harness_config = harness_config_block(state)
    entries = state.get(_ENTRIES_KEY)
    entry_records = [entry for entry in entries if isinstance(entry, Mapping)] if isinstance(entries, list) else []
    facts: dict[str, FactValue] = {
        "prep.stage_reached": str(state.get(_PREPARATION_STAGE_KEY) or ""),
        "arm.harness_config.harness": str(harness_config.get("harness") or ""),
        "arm.harness_config.model_choice_switch": str(harness_config.get("model_choice_switch") or ""),
        "arm.harness_config.is_model_confirmed": _optional_bool(harness_config.get("is_model_confirmed")),
        "entries.count": len(entry_records),
        "decider.call_count": _optional_int(state.get(_DECIDER_CALL_COUNT_KEY)),
    }
    # Keyed by position in the accumulated records rather than by each record's own index, which a
    # stepped case restarts on every step.
    for position, record in enumerate(entry_records):
        facts["entries.{}.outcome".format(position)] = str(record.get("outcome") or "")
        facts["entries.{}.exchange_count".format(position)] = _optional_int(record.get("exchange_count"))
    seed_record = state.get(_SEED_KEY)
    if isinstance(seed_record, Mapping):
        facts["seed.build_status"] = str(seed_record.get(_SEED_BUILD_STATUS_KEY) or "")
    return facts


@pure
def _holds_workspace_tree(member_names: Sequence[str]) -> bool:
    """Whether a snapshot's member list is a workspace tree rather than some other archive."""
    stripped = [name.removeprefix("./") for name in member_names]
    return any(name == _SNAPSHOT_ROOT_MEMBER or name.startswith(_SNAPSHOT_ROOT_MEMBER + "/") for name in stripped)


@pure
def _snapshot_facts(sources: StepFactSources, readings: CheckTimeReadings) -> dict[str, FactValue]:
    """`snapshot.bytes` and `snapshot.readable`, on a trial that pulled a snapshot.

    `readable` asks only what a reader of the tarball can tell without the workspace: that it lists
    at all, and that what it lists is a workspace tree.
    """
    byte_count = _optional_int((sources.state or {}).get(_SNAPSHOT_BYTE_COUNT_KEY))
    if sources.state is None or byte_count is None:
        return {}
    if sources.snapshot_path is None:
        return _not_recorded(("snapshot.readable",)) | {"snapshot.bytes": byte_count}
    member_names = readings.snapshot_member_names
    return {
        "snapshot.bytes": byte_count,
        "snapshot.readable": member_names is not None and _holds_workspace_tree(member_names),
    }


# --- the published trajectory ---


@pure
def trajectory_steps(document: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    """The document's steps, or None when it carries no step list at all.

    None is what keeps a file that is JSON but is not an ATIF document from reading healthy: an
    empty step list answers "every tool call has its result" and "the markers match the case" with a
    yes, which is the one reading a malformed record must never produce.
    """
    raw_steps = document.get("steps")
    if not isinstance(raw_steps, list):
        return None
    return [step for step in raw_steps if isinstance(step, Mapping)]


@pure
def _conversation_steps(steps: Sequence[Mapping[str, Any]], first_client_message: str) -> list[Mapping[str, Any]]:
    """The steps from the one carrying the driver's first message on, which is where the greeting
    ends; empty when no step carries it."""
    wanted_message = first_client_message.strip()
    if not wanted_message:
        return []
    turn_one_index = next(
        (
            index
            for index, step in enumerate(steps)
            if step.get("source") == "user" and str(step.get("message") or "").strip() == wanted_message
        ),
        None,
    )
    return [] if turn_one_index is None else list(steps[turn_one_index:])


@pure
def model_name_coverage(agent_steps: Sequence[Mapping[str, Any]]) -> str | None:
    """Whether all, none or some of the agent steps name the model that answered them; None without
    an agent step."""
    if not agent_steps:
        return None
    named_count = sum(1 for step in agent_steps if step.get("model_name"))
    if named_count == len(agent_steps):
        return MODEL_NAME_COVERAGE_ALL
    elif named_count == 0:
        return MODEL_NAME_COVERAGE_NONE
    else:
        return MODEL_NAME_COVERAGE_SOME


@pure
def _trajectory_source(document: Mapping[str, Any]) -> str | None:
    extra = document.get("extra")
    minds_evals_extra = extra.get("minds_evals") if isinstance(extra, Mapping) else None
    source = minds_evals_extra.get("source") if isinstance(minds_evals_extra, Mapping) else None
    return source if isinstance(source, str) else None


@pure
def embedded_worker_names(document: Mapping[str, Any]) -> list[str]:
    """The workers the trajectory embeds, by the name the eval stamped on each."""
    names: set[str] = set()
    for subagent in document.get("subagent_trajectories") or []:
        extra = subagent.get("extra") if isinstance(subagent, Mapping) else None
        worker = extra.get("worker") if isinstance(extra, Mapping) else None
        name = worker.get("name") if isinstance(worker, Mapping) else None
        if isinstance(name, str) and name:
            names.add(name)
    return sorted(names)


@pure
def is_every_call_answered_once(steps: Sequence[Mapping[str, Any]]) -> bool:
    """Whether every tool call the document records is answered by exactly one result.

    Both directions matter: a call with no result is output the judged transcript and the failure
    scan never see, and a result with no call is a reading attributed to nothing.
    """
    call_ids: list[str] = []
    result_ids: list[str] = []
    for step in steps:
        raw_calls = step.get("tool_calls")
        for call in raw_calls if isinstance(raw_calls, list) else []:
            if isinstance(call, Mapping):
                call_ids.append(str(call.get("tool_call_id") or ""))
        observation = step.get("observation")
        raw_results = observation.get("results") if isinstance(observation, Mapping) else None
        for result in raw_results if isinstance(raw_results, list) else []:
            if isinstance(result, Mapping):
                result_ids.append(str(result.get("source_call_id") or ""))
    return Counter(call_ids) == Counter(result_ids)


@pure
def boundary_marker_steps(steps: Sequence[Mapping[str, Any]]) -> list[tuple[int, Mapping[str, Any]]]:
    """Each step-boundary marker with its position in the document, in document order."""
    markers: list[tuple[int, Mapping[str, Any]]] = []
    for index, step in enumerate(steps):
        extra = step.get("extra")
        minds_evals_extra = extra.get("minds_evals") if isinstance(extra, Mapping) else None
        if (
            isinstance(minds_evals_extra, Mapping)
            and minds_evals_extra.get("kind") == trajectory_reading.STEP_BOUNDARY_KIND
        ):
            markers.append((index, minds_evals_extra))
    return markers


@pure
def _is_marker_followed_by_opening(
    steps: Sequence[Mapping[str, Any]], position: int, marker_extra: Mapping[str, Any]
) -> bool | None:
    """Whether the step after a marker is the client message the marker names; None when the marker
    does not name one, which is a marker this cannot answer for."""
    opening_message = marker_extra.get("opening_message")
    if not isinstance(opening_message, str) or not opening_message:
        return None
    following = steps[position + 1] if position + 1 < len(steps) else None
    if following is None:
        return False
    return following.get("source") == "user" and str(following.get("message") or "") == opening_message


@pure
def _trajectory_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """The transcript, tool-call and step-marker facts the published document holds.

    Omitted before the conversation began: a trial that gave up in preparation publishes no document,
    and there is nothing about a transcript to record.
    """
    if (sources.state or {}).get(_PREPARATION_STAGE_KEY) != PreparationStage.CONVERSATION.value:
        return {}
    entries_before = _entries_before(sources)
    document = sources.trajectory
    steps = trajectory_steps(document) if document is not None else None
    if document is None or steps is None:
        cumulative_fact_names = ("steps.trajectory_cumulative",) if entries_before else ()
        return _not_recorded((*_TRAJECTORY_FACT_NAMES, *cumulative_fact_names))
    client_messages = (sources.state or {}).get(_CLIENT_MESSAGES_KEY)
    first_client_message = str(client_messages[0]) if isinstance(client_messages, list) and client_messages else ""
    conversation_steps = _conversation_steps(steps, first_client_message)
    markers = boundary_marker_steps(steps)
    marker_names = [str(extra.get("step_name") or "") for _position, extra in markers]
    followings = [_is_marker_followed_by_opening(steps, position, extra) for position, extra in markers]
    facts: dict[str, FactValue] = {
        "transcript.source": _trajectory_source(document),
        "transcript.user_step_count": sum(1 for step in conversation_steps if step.get("source") == "user")
        if first_client_message
        else None,
        "transcript.agent_steps_with_model_name": model_name_coverage(
            [step for step in conversation_steps if step.get("source") == "agent"]
        ),
        "tools.every_call_has_one_result": is_every_call_answered_once(steps),
        "workers.embedded": embedded_worker_names(document),
        "steps.boundary_markers": marker_names,
        # A flat trial names no steps and so marks none, which is the agreement it owes.
        "steps.boundary_markers_match_case": marker_names == [name for name in sources.step_names_so_far if name],
    }
    if markers:
        facts["steps.boundary_markers_followed_by_opening"] = (
            None if any(following is None for following in followings) else all(followings)
        )
    if entries_before:
        # Each step's document replays the conversation from its first turn, so a later step's holds
        # at least one turn per entry the earlier steps configured, plus this step's own first.
        facts["steps.trajectory_cumulative"] = (
            sum(1 for step in steps if step.get("source") == "user") >= entries_before + 1
        )
    return facts


# --- where the step sits in its case ---


@pure
def _entries_before(sources: StepFactSources) -> int:
    """How many prompts entries the earlier steps configured; 0 for a flat case or a first step."""
    step = sources.case.step if sources.case is not None else None
    return step.entries_before if step is not None else 0


@pure
def _step_shape_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """`steps.entries_before`, `steps.driver_log_step_local` and `steps.spend_deltas_sum`."""
    facts: dict[str, FactValue] = {}
    if sources.case is None:
        facts["steps.entries_before"] = NOT_RECORDED
    elif sources.case.step is not None:
        facts["steps.entries_before"] = sources.case.step.entries_before
    else:
        # A flat case has no earlier step to have configured entries, so there is nothing to record.
        pass
    if sources.step_index > 0:
        facts["steps.driver_log_step_local"] = _is_driver_log_step_local(sources)
        facts["steps.spend_deltas_sum"] = _do_spend_deltas_sum(sources)
    return facts


@pure
def _is_driver_log_step_local(sources: StepFactSources) -> FactValue:
    """Whether this step's driver log holds only this step's lines.

    The sink is opened and closed per step, so a step-local log begins no earlier than the previous
    step's last line; a cumulative one would begin where the first step's did.
    """
    if not sources.driver_log.is_readable:
        return NOT_RECORDED
    first_timestamp, _last_timestamp = driver_log_timestamps(sources.driver_log.text)
    if not first_timestamp or not sources.previous_driver_log_last_timestamp:
        return None
    return first_timestamp >= sources.previous_driver_log_last_timestamp


@pure
def _do_spend_deltas_sum(sources: StepFactSources) -> FactValue:
    """Whether the per-step spend deltas harbor recorded add up to the trial's own running total.

    The driver publishes each step's share rather than the trial's standing, so a reader summing the
    steps has to arrive at what the step's usage account says the trial has spent.
    """
    if sources.step_metadata is None:
        return NOT_RECORDED
    usage_block = sources.step_metadata.get(_WORKSPACE_USAGE_KEY)
    tokens = usage_block.get("tokens") if isinstance(usage_block, Mapping) else None
    if not isinstance(tokens, Mapping) or any(delta is None for delta in sources.spend_input_token_deltas):
        return None
    cumulative = sum(_optional_int(tokens.get(key)) or 0 for key in ("input", "cache_read", "cache_write"))
    return sum(delta or 0 for delta in sources.spend_input_token_deltas) == cumulative


# --- usage, and what it says about delegated work ---


@pure
def _usage_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """The transcript account's readings: whether it holds tokens at all, whether it accounts for
    every delegation, and the worker launches it counted.

    The transcript account rather than the reported one: a proxy's account holds delegated work whole
    by construction, so its completeness says nothing about the capture.
    """
    if sources.step_metadata is None:
        return _not_recorded(_USAGE_FACT_NAMES)
    usage_block = sources.step_metadata.get(_TRANSCRIPT_USAGE_KEY)
    if not isinstance(usage_block, Mapping):
        return {fact_name: None for fact_name in _USAGE_FACT_NAMES}
    message_count = _optional_int(usage_block.get("message_count"))
    return {
        "usage.tokens_present": None if message_count is None else message_count > 0,
        "usage.is_cost_complete": _optional_bool(usage_block.get("is_cost_complete")),
        "workers.launch_count": _optional_int(usage_block.get("worker_launch_count")),
        "workers.captured_count": _optional_int(usage_block.get("worker_captured_count")),
    }


# --- the evidence manifest and the checks it indexes ---


@pure
def _expectations(sources: StepFactSources) -> ExpandedExpectations | None:
    return sources.case.expectations if sources.case is not None else None


@pure
def _case_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """Whether the step's own instruction could be read.

    It is what says which checks the step owed, so without it every declared check reads as one the
    case never declared -- the silent reading this fact exists to deny, the way `manifest.readable`
    denies the collector's.
    """
    return {"case.readable": sources.case is not None}


@pure
def _is_collection_due(sources: StepFactSources) -> bool:
    """Whether the step had a workspace to collect evidence from. A step that never reached one
    recorded nothing about it, which is not the same claim as a collection that wrote no record."""
    return str((sources.state or {}).get(_PREPARATION_STAGE_KEY) or "") in WORKSPACE_CREATED_STAGES


@pure
def _entries_of_check(entries: Sequence[ManifestEntry], check_class: CheckClass, check_id: str) -> list[ManifestEntry]:
    """A check's entries: the one keyed by its id, or one per target keyed `<check id>_<target>`."""
    return [
        entry
        for entry in entries
        if entry.check_class is check_class
        and (entry.entry_id == check_id or entry.entry_id.startswith("{}_".format(check_id)))
    ]


@pure
def _manifest_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """`manifest.readable`, the entry statuses, and the declared checks' structured readings."""
    expectations = _expectations(sources)
    manifest = sources.manifest
    facts: dict[str, FactValue] = {"manifest.readable": manifest is not None}
    if manifest is None:
        if _is_collection_due(sources):
            facts.update(_not_recorded(("evidence.errored_entries", "evidence.statuses", "apps.seeded")))
        entries: tuple[ManifestEntry, ...] = ()
    else:
        entries = manifest.entries
        facts["evidence.errored_entries"] = sorted(
            collect_error_entry_ids(sources.manifest_block, sources.manifest_path)
        )
        facts["evidence.statuses"] = {entry.entry_id: entry.status.value for entry in entries}
        facts["apps.seeded"] = _seeded_registrations(sources.manifest_block)
    if expectations is None:
        return facts
    is_due = _is_collection_due(sources)
    for http_check in expectations.http_checks:
        check_entries = _entries_of_check(entries, CheckClass.HTTP, http_check.check_id)
        if not check_entries and is_due:
            facts.update(
                _not_recorded(
                    ("http.{}.status".format(http_check.check_id), "http.{}.reason".format(http_check.check_id))
                )
            )
        for entry in check_entries:
            # A check that probed several apps is keyed per app, since one status could not speak
            # for all of them.
            prefix = (
                "http.{}".format(http_check.check_id)
                if len(check_entries) == 1
                else "http.{}.{}".format(http_check.check_id, entry.entry_id[len(http_check.check_id) + 1 :])
            )
            facts["{}.status".format(prefix)] = entry.status_code
            facts["{}.reason".format(prefix)] = entry.reason
    entry_by_id = {entry.entry_id: entry for entry in entries}
    for files_check in expectations.files_checks:
        entry = entry_by_id.get(files_check.check_id)
        if entry is not None:
            facts["files.{}.matched".format(files_check.check_id)] = entry.matched_count
        elif is_due:
            facts["files.{}.matched".format(files_check.check_id)] = NOT_RECORDED
        else:
            pass
    if expectations.test_commands and manifest is not None:
        facts["test_commands.exit_codes"] = [
            entry.exit_code for entry in entries if entry.check_class is CheckClass.TEST_COMMAND
        ]
    elif expectations.test_commands and is_due:
        facts["test_commands.exit_codes"] = NOT_RECORDED
    else:
        pass
    return facts


@pure
def _seeded_registrations(manifest_block: Mapping[str, Any] | None) -> FactValue:
    """The registry rows the case seeded, as the manifest records them; null when it does not say."""
    if manifest_block is None:
        return None
    seeded = manifest_block.get(_SEEDED_REGISTRATIONS_KEY)
    return sorted(str(name) for name in seeded) if isinstance(seeded, list) else None


# --- how long the goal took, and whether the two records of it agree ---


@pure
def _feed_events(sources: StepFactSources) -> list[Mapping[str, Any]]:
    """The workspace events the driver polled, in order, unwrapped from their records.

    A feed event keeps the workspace's own event whole under `event`, because those events carry a
    `type` of their own, so the record's kind and the event's kind are two separate readings.
    """
    events: list[Mapping[str, Any]] = []
    for record in sources.driver_events:
        event = record.get("event")
        if record.get("type") == FEED_EVENT_RECORD_TYPE and isinstance(event, Mapping):
            events.append(event)
    return events


@pure
def _feed_client_message_text(event: Mapping[str, Any]) -> str:
    """One feed event's client-message text, or "" when it is not one.

    Both stream vintages, as the driver's own reader takes them: the ATIF-shaped `step` record with
    `source: "user"` that mngr's emitters write, and the `user_message` record the workspace's chat
    app produces.
    """
    if event.get("type") == "step" and event.get("source") == "user":
        return str(event.get("message") or "").strip()
    if event.get("type") == "user_message":
        return str(event.get("content") or "").strip()
    return ""


@pure
def _feed_agent_reply_text(event: Mapping[str, Any]) -> str:
    """One feed event's agent reply text, or "" when it is not one; both vintages, as above."""
    if event.get("type") == "step" and event.get("source") == "agent":
        return str(event.get("message") or "").strip()
    if event.get("type") == "assistant_message":
        return str(event.get("text") or "").strip()
    return ""


@pure
def _parsed_timestamp(raw: Any) -> datetime | None:
    """A recorded UTC ISO 8601 time; None for anything that does not parse as one."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@pure
def _feed_timestamp(event: Mapping[str, Any]) -> datetime | None:
    """When the workspace says the event happened; None when it says nothing readable."""
    return _parsed_timestamp(event.get("timestamp"))


@pure
def _feed_send_index(events: Sequence[Mapping[str, Any]], message_text: str) -> int | None:
    """Where in the feed the driver's send of one client message landed; None when the feed does not
    carry it, which on a stepped case is every message an earlier step sent."""
    wanted = message_text.strip()
    if not wanted:
        return None
    return next((index for index, event in enumerate(events) if _feed_client_message_text(event) == wanted), None)


@pure
def feed_goal_span(
    events: Sequence[Mapping[str, Any]], client_messages: Sequence[str], turn_index: int
) -> tuple[datetime, datetime] | None:
    """Where the workspace's own feed puts the two ends of the measured span: the case's first client
    message, and the reply the goal-holding client ruled on. None when the feed carries neither.

    The reply is the last agent message between the send of client message ``turn_index`` and the
    send after it, so a later turn's reply is never taken for this one's.
    """
    if turn_index < 1 or turn_index > len(client_messages):
        return None
    start_index = _feed_send_index(events, client_messages[0])
    send_index = _feed_send_index(events, client_messages[turn_index - 1])
    if start_index is None or send_index is None:
        return None
    tail = events[send_index + 1 :]
    next_send_index = (
        _feed_send_index(tail, client_messages[turn_index]) if turn_index < len(client_messages) else None
    )
    replies = tail if next_send_index is None else tail[:next_send_index]
    reply_event = next((event for event in reversed(replies) if _feed_agent_reply_text(event)), None)
    started_at = _feed_timestamp(events[start_index])
    replied_at = _feed_timestamp(reply_event) if reply_event is not None else None
    return None if started_at is None or replied_at is None else (started_at, replied_at)


@pure
def _conversation_records(state: Mapping[str, Any]) -> tuple[tuple[EntryRecord, ...], tuple[TurnRecord, ...]] | None:
    """The state's entry and turn records as their models, or None when either is absent or does not
    parse -- which is a state that did not record the conversation, whichever half is missing.

    Both are needed together: the entry records say which reply the client ruled on, and the turn
    records say when the conversation started and when that reply arrived.
    """
    raw_entries = state.get(_ENTRIES_KEY)
    raw_turns = state.get(_TURNS_KEY)
    if not isinstance(raw_entries, list) or not isinstance(raw_turns, list):
        return None
    try:
        return (
            tuple(EntryRecord.model_validate(record) for record in raw_entries),
            tuple(TurnRecord.model_validate(record) for record in raw_turns),
        )
    except ValidationError:
        return None


@pure
def _timing_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """What the trial recorded about the time to the satisfied goal, on a case that declares a
    `timing` block; a case that asks for no time is timed by nothing and records none of this.

    The span is re-derived here with the collector's own reader, over the records the state persists,
    so `turn_index` and `seconds_within_elapsed` say what the manifest's timing entry was measured
    from. `agrees_with_feed` is the independent reading: the same span as the workspace's event feed
    timed it, which is the only record here the driver's own clock did not write.
    """
    expectations = _expectations(sources)
    if expectations is None or not expectations.timing_checks:
        return {}
    # A state that is absent reads as one that recorded no conversation, which is the same miss.
    state = sources.state or {}
    records = _conversation_records(state)
    if records is None:
        return _not_recorded(_TIMING_FACT_NAMES)
    entry_records, turn_records = records
    timing = evidence_collection.measure_goal_satisfaction_timing(entry_records, turn_records)
    if timing is None:
        # The records are there and no client was ever satisfied, so there is no span to speak of.
        return {fact_name: None for fact_name in _TIMING_FACT_NAMES}
    elapsed_seconds = _optional_float(state.get(_ELAPSED_SECONDS_KEY))
    return {
        "timing.turn_index": timing.turn_index,
        "timing.seconds_within_elapsed": None if elapsed_seconds is None else 0.0 < timing.seconds <= elapsed_seconds,
        "timing.agrees_with_feed": _agrees_with_feed(sources, turn_records, timing),
    }


@pure
def _agrees_with_feed(
    sources: StepFactSources, turn_records: Sequence[TurnRecord], timing: GoalSatisfactionTiming
) -> bool | None:
    """Whether the span the driver measured is the exchange the workspace's feed shows; null when the
    feed carries neither end of it.

    The two records do NOT time the same thing, so their lengths are not comparable: the feed stamps
    the last agent message, while the driver's span closes when the agent is next seen idle, which is
    everything the harness still does after that message plus up to a poll interval. What they do
    agree on is which exchange was measured -- the driver's span opens on the client message the feed
    stamps, and closes at or after the reply the feed stamps -- and that is what a span anchored on
    the welcome turn or on the trial's start does not satisfy.
    """
    client_messages = (sources.state or {}).get(_CLIENT_MESSAGES_KEY)
    span = feed_goal_span(
        _feed_events(sources),
        [str(message) for message in client_messages] if isinstance(client_messages, list) else [],
        timing.turn_index,
    )
    anchored_at = _parsed_timestamp(turn_records[0].sent_at) if turn_records else None
    if span is None or anchored_at is None:
        return None
    feed_started_at, feed_replied_at = span
    closed_at = anchored_at + timedelta(seconds=timing.seconds)
    return (
        abs((feed_started_at - anchored_at).total_seconds()) <= FEED_ROUND_TRIP_SECONDS
        and (feed_replied_at - closed_at).total_seconds() <= FEED_ROUND_TRIP_SECONDS
    )


# --- the app registry and its services ---


@pure
def _app_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """`apps.delivered`, each registered row's service state, and whether every one of them is up.

    Omitted on a step that collected nothing; not recorded when the collection ran and the registry
    capture or the manifest that says what was pre-existing is missing; null when both are there and
    the delivered set still cannot be resolved, which is the `preexisting_unknown` reading the
    collector already records against the entries.
    """
    if not _is_collection_due(sources):
        return {}
    if not sources.apps_registry.is_present or sources.manifest is None:
        return _not_recorded(("apps.delivered", "apps.registered_all_running"))
    rows = registry_rows(sources)
    if rows is None:
        return {"apps.delivered": None, "apps.registered_all_running": None}
    delivered = evidence_collection.resolve_delivered_apps(rows, sources.isolated_instance_services)
    service_state_by_name = evidence_collection.parse_service_states(sources.services.text)
    program_by_registration = evidence_collection.parse_supervised_registrations(sources.supervisord_conf.text)
    facts: dict[str, FactValue] = {"apps.delivered": sorted(app.name for app in delivered)}
    states = [_service_state(app, service_state_by_name, program_by_registration) for app in rows]
    for app, state in zip(rows, states, strict=True):
        facts["apps.service_state.{}".format(app.name)] = state
    facts["apps.registered_all_running"] = (
        None if not service_state_by_name else all(state == _RUNNING_SERVICE_STATE for state in states)
    )
    return facts


@pure
def _service_state(
    app: RegisteredApp,
    service_state_by_name: Mapping[str, str],
    program_by_registration: Mapping[str, str],
) -> str | None:
    """What supervisord reports for the program serving one registry row; None when the status
    capture could not be read at all."""
    if not service_state_by_name:
        return None
    # The config join first, then a program named like the row, as the service entries join them.
    program_name = program_by_registration.get(app.name) or app.name
    return evidence_collection.service_state_for(program_name, service_state_by_name)


# --- the delivered repo ---


@pure
def _repo_facts(sources: StepFactSources, readings: CheckTimeReadings) -> dict[str, FactValue]:
    """`repo.*`, `bundle.*` and `seed.in_head`, on a case that commissions a deliverable bundle."""
    expectations = _expectations(sources)
    if expectations is None or not expectations.is_deliverable_bundle_required:
        return {}
    seed_record = (sources.state or {}).get(_SEED_KEY)
    seed_fact_names = ("seed.in_head",) if isinstance(seed_record, Mapping) else ()
    repo_state = sources.repo_state
    if repo_state is None:
        return _not_recorded(
            (
                "repo.agent_commit_count",
                "repo.bootstrap_commit_count",
                "bundle.bytes",
                "bundle.verified",
                *seed_fact_names,
            )
        )
    facts: dict[str, FactValue] = {
        "repo.agent_commit_count": _counted(repo_state.get(_REPO_AGENT_COMMIT_COUNT_KEY)),
        "repo.bootstrap_commit_count": _counted(repo_state.get(_REPO_BOOTSTRAP_COMMIT_COUNT_KEY)),
    }
    if seed_fact_names:
        # The box's own `merge-base --is-ancestor` reading: the base the bundle is cut from is the
        # seeded commit on a seeded trial, so comparing the two SHAs would say nothing about HEAD.
        facts["seed.in_head"] = _optional_bool(repo_state.get(_REPO_SEEDED_IN_HEAD_KEY))
    return facts | _bundle_facts(sources, repo_state, readings)


@pure
def _bundle_facts(
    sources: StepFactSources, repo_state: Mapping[str, Any], readings: CheckTimeReadings
) -> dict[str, FactValue]:
    """`bundle.bytes` and `bundle.verified`. A trial whose tree never moved past the base commits
    nothing, so there is no bundle to cut and nothing to record."""
    commit_count = _counted(repo_state.get(_REPO_COMMIT_COUNT_KEY))
    if commit_count is None:
        return {"bundle.bytes": None, "bundle.verified": None}
    if commit_count == 0:
        return {}
    if sources.bundle_path is None:
        return _not_recorded(("bundle.bytes", "bundle.verified"))
    return {"bundle.bytes": readings.bundle_byte_count, "bundle.verified": readings.is_bundle_verified}


# --- tickets ---


@pure
def _ticket_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """The titles of the tickets the workspace held at collection time, by kind."""
    fact_names = ("tickets.step_titles", "tickets.closed_step_titles", "tickets.regular_titles")
    if not _is_collection_due(sources):
        return {}
    if not sources.is_tickets_present:
        return _not_recorded(fact_names)
    if sources.tickets_failure_reason:
        return {fact_name: None for fact_name in fact_names}
    return {
        "tickets.step_titles": sorted(record.title for record in sources.tickets if record.is_step),
        "tickets.closed_step_titles": sorted(
            record.title for record in sources.tickets if record.is_step and record.status == "closed"
        ),
        "tickets.regular_titles": sorted(record.title for record in sources.tickets if not record.is_step),
    }


# --- workers ---


@pure
def _agent_created_names(inventory: Sequence[WorkerListingEntry]) -> list[str]:
    return sorted(entry.name for entry in inventory if entry.is_agent_created)


@pure
def _preserved_worker_names(captures: Sequence[CapturedWorkerRecord]) -> set[str]:
    """Workers a complete listing did not name but whose stream mngr still held: destroyed, not
    imagined."""
    return {
        capture.name
        for capture in captures
        if capture.state == WorkerState.DESTROYED.value and capture.is_stream_captured
    }


@pure
def _worker_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """What the step's own records say about the workers the agent launched, and whether the three
    independent lists of them agree.

    Every agreement is null on an incomplete listing: `mngr list` answers with the agents it reached
    and says what it could not, so only a complete listing lets a worker's absence mean anything.
    """
    if not _is_collection_due(sources):
        return {}
    facts: dict[str, FactValue] = {
        "listing.complete": NOT_RECORDED
        if sources.worker_listing_status is None
        else sources.worker_listing_status.is_complete
    }
    is_listing_complete = (
        sources.worker_listing_status is not None
        and sources.worker_listing_status.is_complete
        and sources.is_agent_listing_present
    )
    listed_names = _agent_created_names(sources.agent_inventory)
    facts["workers.listed_agent_created"] = NOT_RECORDED if not sources.is_agent_listing_present else listed_names
    if not sources.is_worker_captures_present:
        return facts | _not_recorded(
            (
                "workers.discovered",
                "workers.captured",
                "workers.no_phantoms",
                "workers.discovered_equals_listed",
                "workers.captured_equals_listed",
                "workers.harness_is_lead_harness",
            )
        )
    captures = sources.worker_captures
    discovered = sorted(capture.name for capture in captures)
    captured = sorted(capture.name for capture in captures if capture.is_stream_captured)
    every_listed_name = {entry.name for entry in sources.agent_inventory}
    preserved = _preserved_worker_names(captures)
    facts.update(
        {
            "workers.discovered": discovered,
            "workers.captured": captured,
            "workers.no_phantoms": all(name in every_listed_name or name in preserved for name in discovered)
            if is_listing_complete
            else None,
            "workers.discovered_equals_listed": discovered == listed_names if is_listing_complete else None,
            "workers.captured_equals_listed": captured == listed_names if is_listing_complete else None,
            "workers.harness_is_lead_harness": _is_worker_harness_the_lead_harness(sources),
        }
    )
    if sources.trajectory is not None:
        facts["workers.embedded_equals_listed"] = (
            embedded_worker_names(sources.trajectory) == listed_names if is_listing_complete else None
        )
    return facts


@pure
def _is_worker_harness_the_lead_harness(sources: StepFactSources) -> bool | None:
    """Whether every worker the listing created runs the harness the lead was signed in on; null
    where the listing named no worker or the arm names no harness."""
    lead_harness = str(harness_config_block(sources.state).get("harness") or "")
    worker_types = [entry.agent_type for entry in sources.agent_inventory if entry.is_agent_created]
    if not lead_harness or not worker_types:
        return None
    return all(is_same_harness(agent_type, lead_harness) for agent_type in worker_types)


# --- UI flows ---


@pure
def observed_kind(observed: str) -> str | None:
    """Which summary a record's `observed` is; a real change, with or without the still-changing
    prefix, is `changed`. None for a record that observed nothing."""
    if not observed:
        return None
    elif observed == ui_flows.UNCHANGED_STATE_SUMMARY:
        return OBSERVED_KIND_UNCHANGED
    elif observed == ui_flows.NO_REACTION_SUMMARY:
        return OBSERVED_KIND_NO_REACTION
    elif observed == ui_flows.ACKNOWLEDGED_ONLY_SUMMARY:
        return OBSERVED_KIND_ACKNOWLEDGED_ONLY
    elif observed == ui_flows.STILL_CHANGING_UNCHANGED_SUMMARY:
        return OBSERVED_KIND_STILL_CHANGING_UNCHANGED
    else:
        return OBSERVED_KIND_CHANGED


class PageReading(FrozenModel):
    """What one recorded page state shows, in the terms the flow facts are stated in."""

    checkboxes: tuple[str, ...] = Field(description="Every checkbox's name, in page order; empty for a nameless one")
    checked: tuple[str, ...] = Field(description="The names among them marked checked, in page order")
    url_query: str | None = Field(description="The page URL's query string; None when the state names no URL")


@pure
def _unescaped(value: str) -> str:
    return _ESCAPE_PATTERN.sub(r"\1", value)


@pure
def read_page_state(state_text: str) -> PageReading:
    """A recorded page state (`page <url> (<title>)` over its ARIA tree) as checkboxes, checked names
    and the URL's query, with the recorder's per-line suffixes ignored."""
    lines = state_text.splitlines()
    page_match = _PAGE_LINE_PATTERN.match(lines[0]) if lines else None
    checkboxes: list[str] = []
    checked: list[str] = []
    for raw_line in lines[1:]:
        checkbox_match = _CHECKBOX_LINE_PATTERN.match(ui_flows.comparable_line(raw_line))
        if checkbox_match is None:
            continue
        name = _unescaped(checkbox_match.group("name") or "")
        checkboxes.append(name)
        if _CHECKED_ATTRIBUTE_PATTERN.search(checkbox_match.group("rest")) is not None:
            checked.append(name)
    return PageReading(
        checkboxes=tuple(checkboxes),
        checked=tuple(checked),
        url_query=urlsplit(page_match.group("url")).query if page_match is not None else None,
    )


@pure
def _after_state_facts(prefix: str, step_index: int, state_text: str) -> dict[str, FactValue]:
    reading = read_page_state(state_text)
    after_prefix = "{}after.{}.".format(prefix, step_index)
    return {
        after_prefix + "checkboxes": list(reading.checkboxes),
        after_prefix + "checked": list(reading.checked),
        after_prefix + "url_query": reading.url_query,
    }


@pure
def flow_facts(
    slug: str, status: str, records: Sequence[Mapping[str, Any]], frame_count: int, verifier_call_count: int | None
) -> dict[str, FactValue]:
    """One flow's facts: its status, and what its log and its frames record.

    A record's `state` is the page before its action, so the page after action k is the next
    record's: the `init` record's for the opening navigation (k = 0), and the closing `done` record's
    for the last action. `png_count` is the frames on disk rather than the frames the log claims,
    since a frame the log names and the directory lacks is exactly the gap worth catching.
    """
    prefix = "flow.{}.".format(slug)
    init_record = next(
        (record for record in records if record.get("kind") == ui_flows.FlowRecordKind.INIT.value), None
    )
    performed_records = [
        record
        for record in records
        if record.get("kind") == ui_flows.FlowRecordKind.ACTION.value
        and record.get("action") not in (_DONE_ACTION_TEXT, ui_flows.UNUSABLE_ACTION)
    ]
    reactions = [str(record.get("reaction") or "") for record in performed_records]
    facts: dict[str, FactValue] = {
        prefix + "status": status,
        prefix + "record_kinds": [str(record.get("kind") or "") for record in records],
        prefix + "png_count": frame_count,
        prefix + "verifier_call_count": verifier_call_count,
        prefix + "reaction_counts": dict(sorted(Counter(reactions).items())),
        prefix + "no_reaction_share": round(reactions.count(_NO_REACTION_VALUE) / len(reactions), _SHARE_DIGITS)
        if reactions
        else None,
        prefix + "ref_step_count": sum(1 for record in performed_records if record.get("target_ref")),
    }
    for record in performed_records:
        step_prefix = "{}step.{}.".format(prefix, record.get("step_index"))
        facts[step_prefix + "reaction"] = str(record.get("reaction") or "")
        facts[step_prefix + "observed_kind"] = observed_kind(str(record.get("observed") or ""))
        facts[step_prefix + "addressed_by_ref"] = bool(record.get("target_ref"))
    state_by_step_index = {
        record.get("step_index"): str(record.get("state") or "")
        for record in records
        if record.get("kind") != ui_flows.FlowRecordKind.INIT.value
    }
    if init_record is not None:
        facts.update(_after_state_facts(prefix, 0, str(init_record.get("state") or "")))
    for record in performed_records:
        step_index = _optional_int(record.get("step_index")) or 0
        after_state = state_by_step_index.get(step_index + 1)
        if after_state is not None:
            facts.update(_after_state_facts(prefix, step_index, after_state))
    return facts


@pure
def _flow_status(flow: FlowSources, manifest_status: str) -> str:
    """The flow's outcome: its own closing run record, or the manifest entry that indexes it."""
    recorded = str(flow.run.get("status") or "") if flow.run is not None else ""
    return recorded or manifest_status


@pure
def _all_flow_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """Every declared flow's facts. A flow the case did not declare is not read: the directory names
    are the case's own slugs, and an undeclared directory belongs to no check."""
    expectations = _expectations(sources)
    if expectations is None or not expectations.ui_flow_checks:
        return {}
    entry_by_id = {entry.entry_id: entry for entry in sources.manifest.entries} if sources.manifest is not None else {}
    facts: dict[str, FactValue] = {}
    for check in expectations.ui_flow_checks:
        slug = slugify(check.name)
        flow = sources.flows_by_slug.get(slug)
        if flow is None or not flow.is_present:
            facts["flow.{}.status".format(slug)] = NOT_RECORDED
            continue
        entry = entry_by_id.get(check.check_id)
        facts.update(
            flow_facts(
                slug,
                _flow_status(flow, entry.status.value if entry is not None else ""),
                flow.records,
                flow.frame_count,
                _optional_int(flow.run.get("verifier_call_count")) if flow.run is not None else None,
            )
        )
    return facts


# --- what the verifier made of the step ---


@pure
def _gate_results(reward_details: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Each structural gate criterion by name, with whether it scored above zero. A gate deleted or
    renamed is then a miss whatever the others say."""
    return {
        str(criterion.get("name") or ""): criterion_value(criterion) > 0.0
        for criterion in criteria_of_dimension(reward_details, GATES_DIMENSION)
    }


@pure
def is_same_harness(left: str, right: str) -> bool:
    """Whether two records name the same harness.

    Three vocabularies meet here -- the captured document's own `agent.name`, the workspace's
    accounts listing, and `mngr list`'s agent type -- and they differ in how they write a compound
    name (`pi-coding`, `pi_coding`). The claim worth checking is which harness ran, so the separator
    and the case are folded and a genuinely different name still misses.
    """
    return left.lower().replace("_", "-") == right.lower().replace("_", "-")


@pure
def _detected_harness(reward_details: Mapping[str, Any]) -> str | None:
    harness_block = reward_details.get(_HARNESS_BLOCK_KEY)
    name = harness_block.get("name") if isinstance(harness_block, Mapping) else None
    return name if isinstance(name, str) else None


@pure
def _verifier_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """`trial.completed`, the structural gates, the harness the verifier detected, and what the
    judges were handed."""
    facts: dict[str, FactValue] = {"trial.completed": not sources.incompletion_reason}
    reward_details = sources.reward_details
    if reward_details is None:
        facts.update(
            _not_recorded(("gates.held", "gates.results", "harness.detected", "harness.detected_is_lane_harness"))
        )
    else:
        detected = _detected_harness(reward_details)
        lane_harness = str(harness_config_block(sources.state).get("harness") or "")
        facts.update(
            {
                "gates.held": is_gates_dimension_passed(reward_details),
                "gates.results": _gate_results(reward_details),
                "harness.detected": detected,
                "harness.detected_is_lane_harness": is_same_harness(detected, lane_harness)
                if detected and lane_harness
                else None,
            }
        )
    expectations = _expectations(sources)
    if expectations is None or not expectations.ui_flow_checks:
        return facts
    facts["judge.digest_flow_count"] = (
        sum(1 for line in sources.judge_flows_digest.text.splitlines() if line.startswith(_DIGEST_FLOW_HEADING_PREFIX))
        if sources.judge_flows_digest.is_readable
        else NOT_RECORDED
    )
    facts["judge.screenshot_count"] = (
        sum(1 for line in sources.judge_screenshots.text.splitlines() if line.strip())
        if sources.judge_screenshots.is_readable
        else NOT_RECORDED
    )
    facts.update(_digest_ref_facts(sources))
    return facts


@pure
def _digest_ref_facts(sources: StepFactSources) -> dict[str, FactValue]:
    """What the judge was told about controls the page gives no accessible name.

    The renderer marks each step performed by ref and, when any step was, adds the note that says
    how to read those marks, so the two together are what a judge actually sees of a `beside` step.
    """
    if not sources.judge_flows_digest.is_readable:
        return _not_recorded(("judge.unnamed_control_note", "judge.addressed_by_ref_step_count"))
    lines = [line.strip() for line in sources.judge_flows_digest.text.splitlines()]
    return {
        "judge.unnamed_control_note": any(line.startswith(_DIGEST_UNNAMED_CONTROL_NOTE_PREFIX) for line in lines),
        "judge.addressed_by_ref_step_count": sum(1 for line in lines if line.startswith(_DIGEST_REF_STEP_PREFIX)),
    }


# --- the block ---


@pure
def compute_step_facts(sources: StepFactSources, readings: CheckTimeReadings) -> dict[str, FactValue]:
    """One step's whole fact block, from dotted fact name to what the step recorded."""
    return {
        **_case_facts(sources),
        **_state_facts(sources),
        **_snapshot_facts(sources, readings),
        **_trajectory_facts(sources),
        **_step_shape_facts(sources),
        **_usage_facts(sources),
        **_manifest_facts(sources),
        **_timing_facts(sources),
        **_app_facts(sources),
        **_repo_facts(sources, readings),
        **_ticket_facts(sources),
        **_worker_facts(sources),
        **_all_flow_facts(sources),
        **_verifier_facts(sources),
        **behaviour_facts.compute_behaviour_facts(sources),
    }
