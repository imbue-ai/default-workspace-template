from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto
from functools import cached_property
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Final
from typing import Self
from typing import assert_never

from pydantic import Field
from pydantic import GetCoreSchemaHandler
from pydantic import JsonValue
from pydantic import StrictFloat
from pydantic import StrictInt
from pydantic import StringConstraints
from pydantic import computed_field
from pydantic import model_validator
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.imbue_common.primitives import PositiveInt
from imbue.imbue_common.pure import pure
from imbue.minds_evals.errors import CapturedFileError
from imbue.minds_evals.errors import EvalConfigError
from imbue.minds_evals.errors import ExpectedFactsTableError
from imbue.minds_evals.errors import ScriptedFlowActionError

# A prompts entry equal to this sentinel is role-played by the decider model
# instead of being sent verbatim. It cannot be the first prompt (there is no
# transcript to decide from yet).
DECIDE_SENTINEL: Final[str] = "DECIDE_FROM_PERSONA"

# What a goal entry gets when it does not say, and the hard ceiling any entry may ask for. Each
# exchange is a full agent turn in a real workspace, so an unbounded budget is not on offer; the cap
# is what keeps a mis-authored case from spending a whole trial budget on one entry.
DEFAULT_MAX_EXCHANGES: Final[int] = 3
MAX_EXCHANGES_CAP: Final[int] = 8

# The default-workspace-template (dwt) each eval case is cloned from.
DEFAULT_DWT_REPO: Final[str] = "https://github.com/imbue-ai/default-workspace-template.git"
DEFAULT_DWT_BRANCH: Final[str] = "main"

DEFAULT_TIMEOUT_SECONDS: Final[float] = 3600.0

# Wall-clock the driver's evidence-collection phase gets after the conversation
# ends; overridable per eval config via "verification_timeout_seconds".
#
# Sized for a case that declares UI flows, which dominate the phase: each flow is bounded
# separately and the rest of the capture takes ~2 minutes. It is a deadline, not a reservation --
# a case with no flows finishes in a couple of minutes and the rest is never spent -- so the
# generous default costs nothing beyond a longer harbor agent timeout.
DEFAULT_VERIFICATION_TIMEOUT_SECONDS: Final[float] = 1800.0

# What "kind": "minds-app" implies when nothing refines it: one delivered app,
# answering 200 on its root path.
DEFAULT_MIN_REGISTERED_APPS: Final[int] = 1
MINDS_APP_EXPECTED_HTTP_STATUS: Final[int] = 200

# The http-check target that fans out to every delivered or seeded app rather
# than naming one service.
REGISTERED_APPS_HTTP_TARGET: Final[str] = "registered-apps"


class DeliverableKind(UpperCaseStrEnum):
    """The shape of artifact a case commissions; each kind implies a standard set of checks."""

    MINDS_APP = auto()


class HttpExpectation(FrozenModel):
    """One authored HTTP probe: what to hit and what the response must look like."""

    target: str = Field(description="'registered-apps' (fan out to every delivered or seeded app) or a service name")
    expect_status: int = Field(description="The HTTP status the probe must observe")
    expect_body_regex: str = Field(description="Regex the captured body head must match (empty means unchecked)")


class FilesExpectation(FrozenModel):
    """One authored file-inventory expectation: a glob that must match at least so many delivered files."""

    glob: str = Field(description="Glob matched against paths relative to the workspace home tree")
    min_count: int = Field(description="How many inventory entries the glob must match")


class DeliverableExpectation(FrozenModel):
    """What a case commissions: a kind whose implied checks the optional fields refine."""

    kind: DeliverableKind = Field(description="The deliverable shape, which implies a standard check set")
    min_registered_apps: int | None = Field(description="Override for how many delivered apps must be registered")
    http: tuple[HttpExpectation, ...] = Field(description="Probes added on top of the kind's implied ones")
    files: tuple[FilesExpectation, ...] = Field(description="Inventory globs added on top of the kind's implied ones")


class FlowSurface(LowerCaseStrEnum):
    """Where a flow enters the delivered app.

    ORIGIN goes straight to the app's forwarded origin -- its own label on the workspace's
    agent-keyed origin, where the proxy serves it -- which is one origin with no frame-piercing and
    exercises the real serving path (forward proxy, tunnel, label origin, the proxy's family-scoped
    session cookie).

    The reserved `minds-ui` surface, which drives the Minds client UI and reaches the app as an
    embedded iframe, has no member here on purpose: it is rejected by name at parse time, so it can
    never be represented as a value the collector might try to act on.
    """

    ORIGIN = auto()


# Spelled with a dash in the config, like the deliverable kinds. Rejected by name rather than
# silently falling into "unknown surface", so a case author gets told it is coming rather than
# misspelled.
RESERVED_MINDS_UI_SURFACE: Final[str] = "minds-ui"


class FlowStartPath(str):
    """Where on its app's origin a UI flow opens: empty for the root, else a path or a query there."""

    def __new__(cls, value: str) -> Self:
        # Joined onto the origin a flow grades, so it must not be able to name another one. It opens
        # with `/` or `?`, never `//`, which is a reference to another host. Browsers read a backslash
        # as a slash and strip tabs and newlines inside a URL, either of which can make `//` out of
        # something else, so every character is printable ASCII other than a backslash.
        is_printable_ascii = all("!" <= character <= "~" and character != "\\" for character in value)
        if value and (value[0] not in "/?" or value.startswith("//") or not is_printable_ascii):
            raise InvalidPrimitiveValueError(
                "{!r} is not a start path: it must be empty or begin with '/' or '?' (not '//'), and hold only "
                "printable ASCII with no spaces or backslashes".format(value)
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema(), serialization=core_schema.to_string_ser_schema()
        )


class ScriptedFlowActionKind(LowerCaseStrEnum):
    """What one action of a scripted UI flow does: the verification agent's own action vocabulary,
    less `done`, which closes every script without being written."""

    CLICK = auto()
    INPUT = auto()
    KEYS = auto()
    SCROLL = auto()
    OPEN = auto()
    RELOAD = auto()
    WAIT = auto()


@pure
def scripted_flow_action_fields(kind: ScriptedFlowActionKind) -> tuple[str, ...]:
    """The fields an action of this kind needs set; every other field must be left empty or zero. A
    `target` is the element's address, which an element with no accessible name gives by `beside`."""
    match kind:
        case ScriptedFlowActionKind.CLICK:
            return ("role", "target")
        case ScriptedFlowActionKind.INPUT:
            return ("role", "target", "text")
        case ScriptedFlowActionKind.KEYS | ScriptedFlowActionKind.OPEN:
            return ("text",)
        case ScriptedFlowActionKind.SCROLL:
            return ("amount",)
        case ScriptedFlowActionKind.RELOAD | ScriptedFlowActionKind.WAIT:
            return ()
        case _ as unreachable:
            assert_never(unreachable)


@pure
def scripted_flow_action_problem(
    kind: ScriptedFlowActionKind, role: str, target: str, beside: str, text: str, amount: int
) -> str:
    """Why these fields do not make an action of this kind, or empty when they do.

    A field the kind does not take is refused rather than ignored: a `text` on a click is an author
    who meant `input`, and running the click would record a flow that never typed anything.
    """
    is_set_by_field = {
        "role": bool(role.strip()),
        "target": bool(target.strip()),
        "beside": bool(beside.strip()),
        "text": bool(text.strip()),
        "amount": amount != 0,
    }
    needed_fields = scripted_flow_action_fields(kind)
    is_element_action = "target" in needed_fields
    if is_element_action and is_set_by_field["target"] and is_set_by_field["beside"]:
        return "{!r} addresses its element by target or by beside, not both".format(kind.value)
    is_needed_field_set = {**is_set_by_field, "target": is_set_by_field["target"] or is_set_by_field["beside"]}
    missing_fields = [field for field in needed_fields if not is_needed_field_set[field]]
    if missing_fields:
        return "{!r} needs {}".format(
            kind.value, " and ".join("target or beside" if field == "target" else field for field in missing_fields)
        )
    taken_fields = {*needed_fields, "beside"} if is_element_action else set(needed_fields)
    unused_fields = [field for field, is_set in is_set_by_field.items() if is_set and field not in taken_fields]
    if unused_fields:
        return "{!r} does not take {}".format(kind.value, " or ".join(unused_fields))
    if kind is ScriptedFlowActionKind.OPEN:
        # A script cannot know the origin its app is forwarded at, so an open names a place on that
        # origin, under the rule that keeps a flow's start path on the app it grades.
        try:
            FlowStartPath(text)
        except InvalidPrimitiveValueError as error:
            return "'open' takes a start path on the app's origin as its text: {}".format(error)
    return ""


class ScriptedFlowAction(FrozenModel):
    """One action of a scripted UI flow, in the executor's own vocabulary, performed exactly as written."""

    kind: ScriptedFlowActionKind = Field(description="Which browser operation to perform")
    role: str = Field(default="", description="The target element's ARIA role, for a click or an input")
    target: str = Field(default="", description="The target element's accessible name, for a click or an input")
    # A snapshot ref is assigned when the page is read, so a script cannot carry one. An element the
    # page gives no accessible name is located instead by text its parent element also holds, and its
    # ref is read off the page state the step is decided on.
    beside: str = Field(
        default="",
        description="For a click or an input on an element of `role` with no accessible name: text a line "
        "under the same parent element holds",
    )
    text: str = Field(
        default="", description="Text to type, keys to press, or the start path on the app's origin to open"
    )
    amount: int = Field(default=0, description="Scroll distance in pixels (negative scrolls up), for a scroll")

    @model_validator(mode="after")
    def _validate_fields_for_kind(self) -> Self:
        problem = scripted_flow_action_problem(
            kind=self.kind,
            role=self.role,
            target=self.target,
            beside=self.beside,
            text=self.text,
            amount=self.amount,
        )
        if problem:
            raise ScriptedFlowActionError(problem)
        return self


class UiFlow(FrozenModel):
    """One behavioral flow through the delivered UI, exactly as authored."""

    name: str = Field(description="Stable flow name; names the flow's evidence directory")
    actions: str = Field(
        description="What to do in the UI, in natural language (empty when the flow carries a script)"
    )
    expect: str = Field(description="The verifiable end condition")
    script: tuple[ScriptedFlowAction, ...] = Field(
        description="The actions a scripted flow performs, in order (empty for a model-driven flow)"
    )
    surface: FlowSurface = Field(description="Where the flow enters the app; defaults to the forwarded origin")
    start_path: FlowStartPath = Field(description="Where on the app's origin the flow opens; empty for its root")


# The most flow screenshots a case may attach to its outcome judge request. The screenshots are that
# request's only images, and the Anthropic Messages API refuses a request carrying more than 100. Past
# 20 images it also limits each image to 2000 px a side, which only shrinks a frame larger than that.
MAX_JUDGE_SCREENSHOTS_CEILING: Final[int] = 100


# What a skill name may be, in a config and in the trajectory it is matched against: a plain name,
# or the `plugin:skill` a skill contributed by a plugin is spelled with. The unanchored expression is
# what a search for a skill path inside a command uses; the anchored pattern validates a whole
# authored name.
SKILL_NAME_EXPRESSION: Final[str] = r"[A-Za-z0-9][A-Za-z0-9._:-]*"
SKILL_NAME_PATTERN: Final[str] = "^{}$".format(SKILL_NAME_EXPRESSION)


@pure
def is_same_skill(requested: str, invoked: str) -> bool:
    """Whether an invocation names the skill a check asks about.

    A skill a plugin contributes is invoked under its qualified name (`frontend-design:frontend-design`
    for the design skill the template enables through a claude plugin), and a config may ask for it
    either way: a qualified name has to match exactly, while a bare one also matches an invocation
    whose skill part, after the last colon, is that name.
    """
    if requested == invoked:
        return True
    if ":" in requested:
        return False
    return invoked.rsplit(":", 1)[-1] == requested


class CheckClass(LowerCaseStrEnum):
    """Which expanded expectation class a manifest entry belongs to; the verifier registers one
    programmatic criterion per scored class and ignores the rest.

    Declared above the authored expectation classes because one of them names classes: a timing
    block's prerequisite is a list of these.
    """

    APP = auto()
    HTTP = auto()
    FILES = auto()
    BUNDLE = auto()
    TEST_COMMAND = auto()
    UI_FLOWS = auto()
    PROCESS = auto()
    TIMING = auto()


class ProcessExpectation(FrozenModel):
    """What a case demands of HOW the agent worked, rather than of what it delivered.

    Answered from the chat agent's own captured transcript, which is the whole conversation's: on a
    stepped case a later step's checks are answered by every step so far, since the agent's stream
    carries no boundary a step could be scoped to.
    """

    required_skills: tuple[str, ...] = Field(description="Skills the agent must invoke at least once")
    forbidden_skills: tuple[str, ...] = Field(description="Skills the agent must never invoke")
    max_worker_launches: int | None = Field(
        description="How many background workers the agent may launch; None leaves the count unconstrained"
    )


class TimingExpectation(FrozenModel):
    """How long a case allows for getting its goal-holding client to say the goal is met.

    The measurement is the wall-clock from the case's first client message to the reply the client
    ruled on, scored on a clamped curve that is linear in log time between the two anchors. The
    anchors are per-case and the curve is global, so two trials that each hit their own case's
    ``fast_seconds`` score alike.

    Both anchors live on the model rather than only in the config parser, for the reason
    ``GoalEntry``'s bounds do: the driver re-validates the case config out of instruction.md at
    trial time, and inverted anchors would otherwise score every trial from the negative side of the
    curve with nothing saying so.
    """

    fast_seconds: float = Field(gt=0.0, description="Wall-clock at or under which the trial scores 1.0")
    slow_seconds: float = Field(gt=0.0, description="Wall-clock at or over which the trial scores 0.0")
    requires_no_failures: tuple[CheckClass, ...] = Field(
        description="Classes whose failure zeroes the time; empty means the time stands on its own"
    )

    @model_validator(mode="after")
    def _validate_anchor_order(self) -> Self:
        if self.fast_seconds >= self.slow_seconds:
            raise EvalConfigError(
                "timing fast_seconds ({}) must be below slow_seconds ({})".format(self.fast_seconds, self.slow_seconds)
            )
        return self


class Expectations(FrozenModel):
    """A case's authored outcome expectations, exactly as written in the eval config."""

    outcome: str = Field(description="The prose the outcome judge grades the delivered artifact against")
    deliverable: DeliverableExpectation | None = Field(description="What the case commissions, if anything")
    ui_flows: tuple[UiFlow, ...] = Field(description="Behavioral flows the verification agent drives through the UI")
    test_commands: tuple[str, ...] = Field(description="Commands run in the delivered repo; recorded, never gated")
    is_fresh_env_enabled: bool = Field(description="Reserved: also boot the deliverable in a fresh workspace")
    max_judge_screenshots: Annotated[int, Field(ge=1, le=MAX_JUDGE_SCREENSHOTS_CEILING)] | None = Field(
        default=None,
        description="How many flow screenshots in all the outcome judge is shown; None keeps the verifier's default",
    )
    process: ProcessExpectation | None = Field(
        default=None, description="How the agent must work, when the case constrains that at all"
    )
    timing: TimingExpectation | None = Field(
        default=None, description="How long the case allows, when it measures that at all"
    )


class AppCheck(FrozenModel):
    """An expanded registry/service check: enough delivered apps are registered and their services run."""

    check_id: str = Field(description="Stable id, used as the manifest entry's id prefix")
    min_registered_apps: int = Field(description="How many delivered apps must appear in the registry")
    is_supervisord_service_required: bool = Field(description="Whether each registered app's service must be running")


class HttpCheck(FrozenModel):
    """An expanded HTTP probe. A 'registered-apps' target fans out to one probe per delivered or seeded app."""

    check_id: str = Field(description="Stable id, used as the manifest entry's id prefix")
    target: str = Field(description="'registered-apps' or a service name")
    expect_status: int = Field(description="The HTTP status the probe must observe")
    expect_body_regex: str = Field(description="Regex the captured body head must match (empty means unchecked)")


class FilesCheck(FrozenModel):
    """An expanded file-inventory check, evaluated at grade time against the captured inventory."""

    check_id: str = Field(description="Stable id, used as the manifest entry's id")
    glob: str = Field(description="Glob matched against paths relative to the workspace home tree")
    min_count: int = Field(description="How many inventory entries the glob must match")


class UiFlowCheck(FrozenModel):
    """An expanded UI flow: one flow driven through the delivered app at trial time.

    A model-driven flow is carried out by the verification agent from its `actions`; a scripted
    flow performs its `script` exactly, and its `actions` are that script rendered as prose, so the
    judge reads both kinds the same way. The reserved `minds-ui` surface is rejected at parse time
    and never reaches here.
    """

    check_id: str = Field(description="Stable id, used as the manifest entry's id")
    name: str = Field(description="The flow's name; names its evidence directory under flows/")
    actions: str = Field(
        description="What to do in the UI, in natural language; for a scripted flow, its script rendered as prose"
    )
    script: tuple[ScriptedFlowAction, ...] = Field(
        description="The actions a scripted flow performs, in order (empty for a model-driven flow)"
    )
    expect: str = Field(description="The verifiable end condition the agent judges the final state against")
    surface: FlowSurface = Field(description="Where the flow enters the app; the forwarded origin in v1")
    start_path: FlowStartPath = Field(description="Where on the app's origin the flow opens; empty for its root")


class ProcessCheckKind(LowerCaseStrEnum):
    """Which question one expanded process check asks of the agent's own transcript."""

    REQUIRED_SKILL = auto()
    FORBIDDEN_SKILL = auto()
    MAX_WORKER_LAUNCHES = auto()


class ProcessCheck(FrozenModel):
    """An expanded process check: one question about how the agent worked, answered at trial time
    from its captured transcript."""

    check_id: str = Field(description="Stable id, used as the manifest entry's id")
    kind: ProcessCheckKind = Field(description="Which question this check asks")
    skill: str = Field(description="The skill the check is about; empty on the worker-launch cap")
    max_worker_launches: int = Field(
        description="How many workers may be launched; 0 on every kind but the worker-launch cap"
    )


class TimingCheck(TimingExpectation):
    """An expanded timing check: the authored anchors plus the id its manifest entry is keyed on.

    One check answers the whole class, so its id needs no discriminator; the id is a constant rather
    than a per-case slug precisely so a reader (and a regrade) finds the entry under one name.
    """

    check_id: str = Field(description="Stable id, used as the manifest entry's id")


class ExpandedExpectations(FrozenModel):
    """The expectations after the generator expands `deliverable.kind` into an explicit check list.

    Both consumers read this exact object -- the driver out of instruction.md, the verifier out of
    tests/case.json -- so the evidence collector can never probe a different set than the judge scores.
    """

    outcome: str = Field(description="The prose the outcome judge grades the delivered artifact against")
    app_checks: tuple[AppCheck, ...] = Field(description="Registry/service checks; scored as app_registered")
    http_checks: tuple[HttpCheck, ...] = Field(description="Probes; scored as http_expectations_met")
    files_checks: tuple[FilesCheck, ...] = Field(description="Inventory globs; scored as files_expectations_met")
    test_commands: tuple[str, ...] = Field(description="Commands run in the delivered repo; recorded, never scored")
    is_deliverable_bundle_required: bool = Field(description="Whether to capture the delivered repo as a git bundle")
    ui_flow_checks: tuple[UiFlowCheck, ...] = Field(
        description="Flows driven through the UI; scored as ui_flows_completed"
    )
    is_fresh_env_enabled: bool = Field(description="Reserved: also boot the deliverable in a fresh workspace")
    # Read by the verifier's render_flow_evidence.py, which keeps its own default when this is None.
    max_judge_screenshots: Annotated[int, Field(ge=1, le=MAX_JUDGE_SCREENSHOTS_CEILING)] | None = Field(
        default=None,
        description="How many flow screenshots in all the outcome judge is shown; None keeps the verifier's default",
    )
    # Defaulted because this object is re-validated out of instruction.md at trial time and out of
    # tests/case.json at grade time: a dataset whose case config carries no process checks must
    # still load.
    process_checks: tuple[ProcessCheck, ...] = Field(
        default=(), description="Checks on how the agent worked; scored as process_expectations_met"
    )
    # A tuple holding at most one check, for uniformity with every other class: the collector and
    # the verifier iterate a class's check list, and a case declares a single timing block.
    timing_checks: tuple[TimingCheck, ...] = Field(
        default=(), description="How long the agent had; scored as time_to_goal_within_expectations"
    )


class CheckStatus(LowerCaseStrEnum):
    """How one recorded probe came out. The distinction the whole grading policy rests on is
    FAILED (the workspace fell short) versus ERROR (the harness could not find out)."""

    PASSED = auto()
    FAILED = auto()
    ERROR = auto()


class EvidenceEnv(LowerCaseStrEnum):
    """Which environment an entry was measured in: the workspace that built the app, or a fresh boot
    of the delivered repo (reserved; phase 1 only ever records LIVE)."""

    LIVE = auto()
    FRESH = auto()


class ManifestEntry(FrozenModel):
    """One recorded probe in the evidence manifest: what was checked, how it came out, and why."""

    entry_id: str = Field(description="Stable id within the trial, e.g. 'http_registered_apps_0'")
    check_class: CheckClass = Field(description="The expanded expectation class this entry feeds")
    status: CheckStatus = Field(description="Passed, fell short, or could not be determined")
    env: EvidenceEnv = Field(description="Which environment the entry was measured in")
    reason: str = Field(description="Why the entry is not PASSED (e.g. 'timeout'); empty when it passed")
    detail: str = Field(description="Bounded human/judge-readable evidence for this entry")
    evidence_path: str = Field(description="Bundle-relative path to this entry's evidence file, if any")
    # The structured readings a reader would otherwise have to parse out of `detail`. Each is set only
    # on the entry kind it belongs to, and None wherever it was not measured.
    exit_code: int | None = Field(
        default=None, description="A test command's exit code; None on other entries and when none was reported"
    )
    status_code: int | None = Field(
        default=None,
        description="The HTTP status a probe observed (0 for a refused connection); None on other entries and "
        "when no probe was taken",
    )
    matched_count: int | None = Field(
        default=None,
        description="How many inventory paths a files check's glob matched; None on other entries and when unmeasured",
    )
    # Defaulted because the manifest is wire data between the collector and the verifier: a bundle
    # written before a class carried a measurement must still load. Most classes are pass/fail and
    # leave this None; a class scored on a curve carries its measurement here so that the collector
    # and the grade-time criterion never compute the same number twice.
    value: float | None = Field(default=None, description="A continuous measurement this entry carries, if any")


class TraceRecord(FrozenModel):
    """One command the evidence collector ran, with its bounded output -- the collector's own flight
    recorder, so a failure can be attributed to the instrument rather than the workspace."""

    timestamp: str = Field(description="UTC ISO timestamp the command was issued")
    phase: str = Field(description="The collection phase that issued the command")
    command: str = Field(description="The command as sent into the workspace or the box")
    is_success: bool = Field(description="Whether the bridge reported the command succeeded")
    output: str = Field(description="Bounded raw output, failures included")


class CapturedFile(FrozenModel):
    """One file the evidence phase tried to bring out of the workspace: where it landed in the host-side
    bundle, or why it did not."""

    host_path: Path | None = Field(description="Where the file landed host-side; None when it was not captured")
    failure_reason: str = Field(description="Why the file was not captured (e.g. 'pull_failed'); empty when it was")
    failure_detail: str = Field(description="Bounded diagnostic for the failure (e.g. a stderr tail); empty otherwise")

    @property
    def is_captured(self) -> bool:
        return self.host_path is not None

    @model_validator(mode="after")
    def _validate_captured_or_failed(self) -> Self:
        if self.host_path is None and not self.failure_reason:
            raise CapturedFileError("an uncaptured file must name a failure reason")
        if self.host_path is not None and (self.failure_reason or self.failure_detail):
            raise CapturedFileError("a captured file cannot also carry a failure")
        return self


class WorkerState(LowerCaseStrEnum):
    """A background worker's state at collection time: the listing's lifecycle state folded down when
    the worker is listed, DESTROYED when a complete listing did not name it but its stream still came
    out of mngr's archive, and UNKNOWN whenever that falls short -- no listing that could speak for
    it, or no stream to show for its absence."""

    STOPPED = auto()
    RUNNING = auto()
    DESTROYED = auto()
    UNKNOWN = auto()


class WorkerLaunch(FrozenModel):
    """One background worker an agent's own stream shows it creating through the launch-task skill."""

    name: str = Field(description="The worker's mngr agent name, from the launch command's --name")
    tool_call_id: str = Field(description="The tool call that ran the launch command; where the worker embeds")
    task_file: str = Field(
        description="The --task-file path as the launch wrote it (relative to the lead's work dir unless absolute), or empty"
    )
    depth: int = Field(description="0 for a worker the chat agent launched, 1 for a worker's worker, and so on")
    lead_name: str = Field(description="The worker that launched it; empty when the chat agent did")


class SkillInvocation(FrozenModel):
    """One skill an agent's own stream shows it invoking, however its harness spells the invocation."""

    name: str = Field(description="The skill's name, as the invocation spells it")
    tool_call_id: str = Field(description="The tool call that invoked it; empty when the record carries no id")
    step_id: str = Field(description="The step the call belongs to; empty when the record carries no step id")


class WorkerListingEntry(FrozenModel):
    """One agent as `mngr list --format json` reported it inside the workspace at collection time."""

    agent_id: str = Field(description="The mngr agent id")
    name: str = Field(description="The agent name")
    agent_type: str = Field(description="The agent type (claude, codex, ...)")
    state: WorkerState = Field(description="The lifecycle state, folded into what the capture cares about")
    work_dir: str = Field(description="The agent's work dir, which launch commands' paths are relative to")
    # The launch-task flow stamps `agent_created=true` on every worker it creates, which is the only
    # field that identifies a worker without reading the lead's command text.
    labels: Mapping[str, str] = Field(description="The agent's mngr labels, as the listing reported them")

    @property
    def is_agent_created(self) -> bool:
        """Whether mngr recorded this agent as created by another agent rather than by a person."""
        return str(self.labels.get("agent_created", "")).lower() == "true"


class TicketRecord(FrozenModel):
    """One `tk` ticket as the workspace's `data/.tickets/<id>.md` file records it.

    Serialized with the ticket file's own key names (`id`, `type`), which is the shape
    `verification/tickets.jsonl` carries.
    """

    ticket_id: str = Field(serialization_alias="id", description="The ticket id; the file name's stem when unstated")
    ticket_type: str = Field(serialization_alias="type", description="The ticket type (task, chore, ...)")
    status: str = Field(description="open, in_progress or closed, as tk wrote it")
    is_step: bool = Field(description="Whether the ticket is a turn-bound progress step (`step: true`)")
    agent: str = Field(description="The mngr agent that created the ticket; empty outside an mngr agent")
    title: str = Field(description="The `# ` heading's text")
    summary: str = Field(description="The `## Summary` section's text, which tk writes on close; empty when absent")
    created: str = Field(description="The creation timestamp as tk wrote it")
    closed: str = Field(description="The close timestamp as tk wrote it; empty for a ticket still open")


class TicketCapture(FrozenModel):
    """What the evidence phase read out of the workspace's `tk` ticket directory."""

    records: tuple[TicketRecord, ...] = Field(description="Every ticket file that parsed, in file-name order")
    is_directory_present: bool = Field(description="Whether the tickets directory existed at all")
    unparsed_file_names: tuple[str, ...] = Field(
        description="Ticket files that could not be read or carry no frontmatter, left out of the records"
    )
    omitted_file_count: int = Field(description="Ticket files the capture's size bound left unread")
    failure_reason: str = Field(description="Why the capture as a whole failed (e.g. 'bridge_failed'); empty when not")
    failure_detail: str = Field(description="Bounded diagnostic for that failure; empty otherwise")


class DiagnosticProbeCapture(FrozenModel):
    """What the self-diagnostic probe printed at collection, kept raw: it is parsed only by
    `diagnostic_probe.py`, which shares nothing with the collectors it is a second record for."""

    output: str = Field(description="The probe's output exactly as the exec returned it; empty when it failed")
    failure_reason: str = Field(description="Why the exec did not answer (e.g. 'bridge_failed'); empty when it did")


class ProbeTicket(FrozenModel):
    """One ticket file as the probe printed it."""

    title: str = Field(description="The first `# ` heading's text")
    status: str = Field(description="The frontmatter's status")
    is_step: bool = Field(description="Whether the frontmatter says `step: true`")


class ProbeAgent(FrozenModel):
    """One agent as the probe's own `mngr list` printed it."""

    name: str = Field(description="The agent name")
    agent_type: str = Field(description="The agent type")
    labels: Mapping[str, str] = Field(description="The agent's labels")


class DiagnosticProbeReading(FrozenModel):
    """The probe's output, parsed: the workspace's tickets, agents, worker reports and uploads."""

    tickets: tuple[ProbeTicket, ...] = Field(description="Every ticket file with frontmatter, in path order")
    agents: tuple[ProbeAgent, ...] | None = Field(
        description="Every agent the listing named; None when the listing could not be read"
    )
    report_paths: tuple[str, ...] = Field(description="Every launch-task report.md, relative to the repo")
    upload_paths: tuple[str, ...] = Field(description="Every file under the uploads directory, relative to the repo")


class PreparationStage(LowerCaseStrEnum):
    """The workspace-preparation stages a trial passes, in the order it passes them.

    `seed_built` is the one stage reached before any workspace exists. A trial with no seed skips
    `seed_built` and `seed_running`, one with no proxy skips `proxied`, and one whose harness config
    names no model skips `switched`; `conversation` is reached once turn 1 has been sent.
    """

    SEED_BUILT = auto()
    CREATED = auto()
    SEED_RUNNING = auto()
    PROXIED = auto()
    SIGNED_IN = auto()
    CHAT_CREATED = auto()
    WELCOMED = auto()
    SWITCHED = auto()
    CONVERSATION = auto()


class WorkerListing(FrozenModel):
    """The workspace's agents as one collection attempt saw them.

    `is_complete` is what licenses reading a worker's *absence* as meaning something: `mngr list`
    defaults to --on-error continue, so it can answer with some agents and a non-zero exit when a
    provider was unreachable. Only a listing that reported every agent it was asked for can say that
    an agent it does not name is gone.
    """

    entries: tuple[WorkerListingEntry, ...] = Field(
        default=(), description="The agents the listing named, in the order it named them"
    )
    is_complete: bool = Field(default=False, description="Whether the listing reported every agent without error")


class SeedBuildStatus(LowerCaseStrEnum):
    """How building a case's seed onto the case base came out."""

    CLEAN = auto()
    # The merge left unmerged paths.
    CONFLICT = auto()
    # The merge was clean, but the merged supervisord config registers the seed on top of something.
    COLLISION = auto()


class SeedBuildRecord(FrozenModel):
    """What the seed build did, as state.json carries it."""

    app_name: str = Field(description="The seeded app's registry and program name")
    build_status: SeedBuildStatus = Field(description="How the build came out")
    seed_commit_sha: str = Field(description="The seed as a commit on the pinned template alone; empty if never made")
    seeded_sha: str = Field(description="The case branch's head once the seed is merged in; empty unless clean")
    conflicted_paths: tuple[str, ...] = Field(description="The paths a conflicting merge left unmerged")
    collisions: tuple[str, ...] = Field(description="What the merged config registers the seed on top of")
    impossible_reason: str = Field(description="Why the seed cannot be built, in prose; empty on a clean build")


class WorkerCapture(FrozenModel):
    """What the evidence phase brought out for one launched worker: its ATIF document, its stream, and the
    report it pushed back to its lead, each recorded on its own."""

    launch: WorkerLaunch = Field(description="The launch this capture answers")
    agent_id: str = Field(description="The worker's mngr agent id; empty when it could not be resolved")
    agent_type: str = Field(
        description="The worker's agent type: the listing's, or the captured document's when the listing "
        "did not name the worker; empty when neither said"
    )
    state: WorkerState = Field(description="The worker's state at collection time")
    document: CapturedFile = Field(description="The ATIF document mngr built for the worker")
    stream: CapturedFile = Field(description="The worker's common-transcript stream, live or preserved")
    report: CapturedFile = Field(description="The lead-side reports directory the worker pushed its report into")


class TranscriptCapture(FrozenModel):
    """What the evidence phase brought out of the workspace agent's common transcript: the raw stream and
    the ATIF document mngr built from it, each recorded on its own."""

    stream: CapturedFile = Field(description="The common-transcript stream, one record per line")
    document: CapturedFile = Field(description="The ATIF trajectory document mngr assembled from the stream")


class TrajectorySource(LowerCaseStrEnum):
    """Which shape the trial's trajectory.json has: the workspace's own ATIF document, the driver's
    hand-built turn summary, or none because there was no conversation to describe."""

    WORKSPACE = auto()
    HAND_BUILT = auto()
    NONE = auto()


class UsageSource(LowerCaseStrEnum):
    """Which account the trial's reported workspace usage was taken from."""

    PROXY = auto()
    TRANSCRIPT = auto()


class RegisteredApp(FrozenModel):
    """One entry of the workspace's app registry (data/.state/apps.toml)."""

    name: str = Field(description="The registered service name")
    url: str = Field(description="The workspace-local origin the app is served on")
    # The unguessable `<name>-<rand>` origin label forward_port.py mints, and the component the
    # forwarded origin is built from: `https://<label>.agent-<hex>.localhost:<port>/`. The forward
    # proxy maps the label back to the service name itself, so the label -- not the name -- is what
    # a URL must carry. Defaulted rather than required because "no label" is a real registry state
    # and not an omission: a row written before labels existed has none, and forward routes it under
    # its own name.
    label: str = Field(default="", description="The service's origin label; empty when the row has none")
    # Measured from the workspace before the first turn, not matched against a hand-kept name list
    # (see `evidence_collection.resolve_preexisting_registrations` for how the set is read).
    is_preexisting: bool = Field(description="Whether the workspace already served this row before the agent ran")
    # Declared by the case's seed rather than measured: nothing observes the workspace before the
    # seed is in it, so the pre-existing measurement sees a seeded row too, and seeded wins. A
    # seeded row is never pre-existing, so the two classes stay disjoint.
    is_seeded: bool = Field(description="Whether the case seeded the workspace with this row before the agent ran")
    # The registry's own `internal = true` marker: machinery that forwards a port but has no page of
    # its own to show, so the workspace never offers it as an app to open.
    is_internal: bool = Field(description="Whether the registry marks this row as not an openable app")


class PhaseTiming(FrozenModel):
    """Wall-clock spent in one collection phase, so a slow or truncated phase is visible after the fact."""

    name: str = Field(description="The collection phase's name")
    seconds: float = Field(description="Wall-clock the phase took")


class EvidenceManifest(FrozenModel):
    """The index of everything the evidence collector recorded: the contract between collection and
    judgment."""

    schema_version: int = Field(description="Bumped when the manifest shape changes incompatibly")
    case_id: str = Field(description="The case the evidence belongs to")
    # What the captured deliverable bundle is based on. A replay regenerates the base clone from the
    # dwt tip, checks it reproduces base_sha, and unbundles the workspace's commits onto it -- which only
    # works because the eval-case commit is made with fixed dates and is therefore reproducible.
    base_sha: str = Field(description="HEAD of the prepared eval-case clone; the git bundle's base")
    dwt_tip_sha: str = Field(description="The workspace-template tip the base clone was made from")
    # What the collector subtracted from the registry to arrive at the delivered set, so a reader
    # can see the exclusion rather than infer it. The manifest itself has to keep "unknown" apart
    # from "the workspace served nothing": a case with no expectations records no entry that would
    # otherwise carry the `preexisting_unknown` reason.
    preexisting_registrations: tuple[str, ...] | None = Field(
        description="Sorted registry names the workspace already served before the agent ran; None if unknown"
    )
    # Whether the registry file was read at all, which an empty capture cannot say on its own: an
    # unreadable registry and a workspace that registered nothing both leave `apps.toml` empty.
    is_registry_present: bool = Field(description="Whether the workspace's app registry could be read")
    # The seeded rows are probed and driven like delivered ones but never counted as delivered; kept
    # beside the pre-existing set so a reader can see both exclusions from the delivered count.
    seeded_registrations: tuple[str, ...] = Field(
        description="Sorted registry names the case seeded the workspace with; empty on an unseeded trial"
    )
    is_expectations_declared: bool = Field(description="Whether the case declared expectations at all")
    is_evidence_complete: bool = Field(description="True when no entry has status ERROR")
    started_at: str = Field(description="UTC ISO timestamp the collection phase began")
    phases: tuple[PhaseTiming, ...] = Field(description="Wall-clock per collection phase")
    entries: tuple[ManifestEntry, ...] = Field(description="Every recorded probe, in collection order")


class GoalEntry(FrozenModel):
    """A prompts entry that expands into a bounded back-and-forth: a goal-holding client keeps
    replying until it is satisfied or its exchange budget runs out."""

    # Both bounds live on the model rather than only in the config parser, so the driver -- which
    # re-validates the case config out of instruction.md at trial time -- enforces what the
    # generator did, on a dataset produced by any version of the generator. An entry with no goal
    # would have a model hold out for nothing.
    goal: str = Field(min_length=1, description="What the client wants out of this stretch of the conversation")
    max_exchanges: int = Field(
        default=DEFAULT_MAX_EXCHANGES,
        ge=1,
        le=MAX_EXCHANGES_CAP,
        strict=True,
        description="Hard ceiling on the client messages this entry may send",
    )


# A literal message, or the DECIDE_FROM_PERSONA sentinel. Bounded on the model for the same reason
# a goal entry's own fields are: an entry with no text has the client spend a full agent turn saying
# nothing. Stripping matches what the config parser does, so a generated dataset validates unchanged.
MessagePrompt = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# What a `prompts` entry may be: a plain string is a literal message or the DECIDE_FROM_PERSONA
# sentinel; an object expands into a bounded goal-driven exchange.
PromptEntry = MessagePrompt | GoalEntry


@pure
def entry_exchange_budget(entry: PromptEntry) -> int:
    """How many client messages one prompts entry may send. A string entry is exactly one turn."""
    return entry.max_exchanges if isinstance(entry, GoalEntry) else 1


class TurnEntryKind(LowerCaseStrEnum):
    """Which kind of turn source a prompts entry resolved to, recorded per entry in state.json."""

    LITERAL = auto()
    PERSONA = auto()
    GOAL = auto()


class DeciderTurn(FrozenModel):
    """One decider-model call a turn source made on the client's behalf: the message it sent, or the
    decision to end its entry without one."""

    turn: int | None = Field(
        description="The 1-based index of the message the call sent; None when it sent none (it ended "
        "the entry, or its message never reached the workspace)"
    )
    entry_index: int = Field(description="The 0-based prompts entry the call belongs to")
    exchange: int = Field(description="The 0-based exchange within that entry")
    entry_kind: TurnEntryKind = Field(description="What kind of entry the source was driving")
    model: str = Field(description="The decider model that answered (the configured model on a fallback)")
    is_fallback: bool = Field(description="Whether the literal fallback message was sent instead")
    detail: str = Field(description="Why the entry ended, in the client's own words; empty for a call that spoke")


class StepBoundary(FrozenModel):
    """Where one step of a multi-step task begins, so the trajectory can mark it.

    A stepped task drives one workspace across several instructions, and every step's trajectory
    replays the conversation from its first turn -- so without a marker the later steps read as one
    undivided conversation. The marker is cosmetic: it becomes a ``system`` step, which the verifier's
    readers (the judge transcript, the structural gates, the wordiness guard) all skip.
    """

    name: str = Field(description="The step's name, as harbor knows it")
    started_at: str = Field(description="ISO 8601 time the driver began the step")
    conversation_index: int = Field(
        description="How many clean-conversation entries preceded the step; the join for the hand-built shape"
    )
    opening_message: str = Field(
        description="The client's first message of the step; the join for the workspace's own document. "
        "Empty when the step ended before the client said anything"
    )


class HarnessLane(LowerCaseStrEnum):
    """A provider lane a workspace can be signed in on with nobody present.

    The lane decides the harness, because that is how the product itself decides it: a chat runs on
    the harness of the lane its account was minted on (`harness_for_lane`). A lane is a member
    exactly when it offers a pasted-key sign-in, because that is the only kind a run can drive; one
    whose every method is a browser or device flow on a PTY needs a person at it, which is what
    leaves `google` (antigravity) out.
    """

    ANTHROPIC = auto()
    OPENAI = auto()
    API_KEY = auto()
    OPENROUTER = auto()
    OPENCODE_GO = auto()


@pure
def lane_id(lane: HarnessLane) -> str:
    """The lane as the workspace's accounts API and the command line spell it: dashes, not
    underscores (`--ak lane=api-key`)."""
    return lane.value.replace("_", "-")


class HarnessName(LowerCaseStrEnum):
    """A harness a workspace chat can run on, one for each kind of lane a run can sign in on."""

    CLAUDE = auto()
    CODEX = auto()
    PI_CODING = auto()


@pure
def harness_id(harness: HarnessName) -> str:
    """The harness as the workspace's accounts listing and agent types spell it: dashes, not
    underscores (`pi-coding`)."""
    return harness.value.replace("_", "-")


@pure
def harness_for_lane(lane: HarnessLane) -> HarnessName:
    """The harness a chat on an account minted on this lane runs, as the workspace template assigns
    it; the pi-coding lanes differ only in whose key they take."""
    match lane:
        case HarnessLane.ANTHROPIC:
            return HarnessName.CLAUDE
        case HarnessLane.OPENAI:
            return HarnessName.CODEX
        case HarnessLane.API_KEY | HarnessLane.OPENROUTER | HarnessLane.OPENCODE_GO:
            return HarnessName.PI_CODING
        case _ as unreachable:
            assert_never(unreachable)


# Lanes whose harness names no model on a transcript step, so a trial on one can never confirm the
# model it asked for. mngr's codex transcript emitter writes no per-step model name, which leaves
# every `openai` trial's observed models empty.
# CLEANUP: drop this set and the two renderers' branches that read it once mngr's codex transcript
# emitter stamps a per-step model name. From then on an empty observation here means what it means
# on every other lane -- a trial whose transcript said nothing -- and treating it as the lane's shape
# would hide a real silence rather than a structural one.
_LANES_WITH_NO_OBSERVABLE_MODEL: Final[frozenset[str]] = frozenset({lane_id(HarnessLane.OPENAI)})


@pure
def is_model_observable_on_lane(lane: str) -> bool:
    """Whether a trial on this lane can say at all which model answered it.

    False is the lane's known shape rather than anything about a given trial, which is why both
    renderers of the confirmation ask this before reporting a model as unconfirmed: a line every
    trial of an arm carries every night is one a reader learns to skim, and that costs the arms
    raising it for a reason.
    """
    return lane not in _LANES_WITH_NO_OBSERVABLE_MODEL


class HarnessConfig(FrozenModel):
    """The harness, model, effort and speed tier one run drives every one of its cases on.

    One half of a run's arm, the other being the (mngr, dwt) pair the trial's box and workspace come
    from. A run-level property like that pair, never a per-case one: it is parsed once from the run's
    agent kwargs and applied to the one workspace each trial prepares.
    """

    lane: HarnessLane = Field(description="The provider lane the workspace is signed in on")
    key_provider: str = Field(description="Which provider the key belongs to; only the api-key lane takes one")
    key_env: str = Field(description="The environment variable holding the lane's key, derived when not given")
    model: str = Field(description="The catalog id to switch the chat to before turn 1; empty means no switch")
    effort: str = Field(description="The effort or thinking level set with the model; empty means no switch")
    is_fast: bool = Field(description="Whether the switch asks for the fast speed tier")

    @property
    def is_switch_requested(self) -> bool:
        """Whether this config asks for a model choice at all. A config that names no model changes
        no setting of the workspace, and the chat runs exactly as the product ships it."""
        return bool(self.model)


class ObservedHarnessModels(FrozenModel):
    """Which models actually answered, as read out of the captured transcript.

    Empty until a transcript has been captured, which is the only place the models are recorded: no
    workspace endpoint reads a chat's live model choice back. (The harness writes it to
    `$MNGR_HOST_DIR/agents/<chat_id>/model_state.json`, which a bridged exec could read; nothing
    here does.)
    """

    models: tuple[str, ...] = Field(
        default=(), description="Sorted model names on the agent steps after the client's first turn"
    )
    welcome_model: str = Field(
        default="", description="The model that answered the create template's greeting, before any switch"
    )


class HarnessConfigRecord(FrozenModel):
    """What harness settings a trial was asked to run on and what it was observed running on.

    The field names are this block's own keys wherever it is serialized, and they are the harness
    config's kwarg names rather than this package's naming conventions -- hence `fast` for the
    requested speed tier, which is what the operator typed.
    """

    lane: str = Field(default="", description="The provider lane the run asked to be signed in on")
    key_provider: str = Field(default="", description="The provider the lane's key belongs to; empty off api-key")
    account_id: str = Field(default="", description="The account the sign-in minted and the chat was created against")
    harness: str = Field(default="", description="The harness, read back from the workspace's accounts listing")
    model: str = Field(default="", description="The catalog id the run asked for; empty when it asked for none")
    effort: str = Field(default="", description="The effort level the run asked for")
    fast: bool = Field(default=False, description="The speed tier the run asked for")
    # Named for the product's own name for the call that sets model, effort and fast together
    # (`POST /api/chats/<chat_id>/model`), since one such call is what this field reports on.
    model_choice_switch: str = Field(
        default="",
        description="'applied', 'skipped' when the config named no model, the failure that stopped the trial, or "
        "empty when the trial gave up before the switch",
    )
    observed_models: tuple[str, ...] = Field(
        default=(), description="The models the transcript shows answering after the client's first turn"
    )
    welcome_model: str = Field(default="", description="The model that answered the greeting, before any switch")
    # None rather than False for a config that requested no switch and for a catalog id whose
    # reported naming is not known: an unrecognised model name is not evidence of a wrong model.
    is_model_confirmed: bool | None = Field(
        default=None,
        description="Whether the observed models are exactly the requested one; None when it cannot be told",
    )


class ArmRecord(FrozenModel):
    """The whole treatment a trial ran under: the (mngr, dwt) pair its box and workspace were built
    from, and the harness config applied on top of that pair.

    Every field is defaulted so a trajectory that no run drove -- the oracle's, built from a canned
    transcript -- carries an empty arm rather than none at all.
    """

    mngr_sha: str = Field(default="", description="The mngr SHA the trial's box was built from")
    dwt_sha: str = Field(default="", description="The workspace-template SHA the trial's workspace was cloned from")
    harness_config: HarnessConfigRecord = Field(
        default_factory=HarnessConfigRecord,
        description="The harness settings the trial was asked to run on and was observed running on",
    )


class TrajectoryProvenance(FrozenModel):
    """What the eval knows about a trial's trajectory that the workspace document cannot: who drove it,
    which decider spoke for the client, which arm -- pinned pair plus harness config -- it ran on,
    and whose account its usage figures come from."""

    driver_name: str = Field(description="The harbor agent that drove the conversation")
    driver_version: str = Field(description="That agent's version")
    decider_model: str = Field(description="The simulated-user model configured for the trial")
    decider_turns: tuple[DeciderTurn, ...] = Field(description="The turns the decider wrote, in order")
    harbor_session_id: str | None = Field(
        description="The harbor session the trial ran under; None when harbor left it unset"
    )
    case_id: str = Field(description="The case the trial ran")
    usage_source: UsageSource = Field(description="Which account final_metrics carries")
    # Defaulted to the empty record for a trajectory no run drove: the oracle builds its own
    # provenance from a canned transcript, pinning nothing and requesting nothing of any workspace.
    arm: ArmRecord = Field(default_factory=ArmRecord, description="The treatment the trial was asked to run under")


class TurnOutcome(LowerCaseStrEnum):
    """Why one prompts entry stopped producing client messages.

    The loop enforces an entry's exchange ceiling, but the source declares what hitting it means
    (`TurnSource.exhaustion_end`): only a goal-holding client is ever really cut off, so only its
    entries end BUDGET_EXHAUSTED. Such an entry is recorded and shown to the outcome judge rather
    than gating the reward to zero, because an agent that cannot satisfy an unreasonable goal is not
    the same thing as a broken trial.
    """

    COMPLETED = auto()
    SATISFIED = auto()
    BUDGET_EXHAUSTED = auto()
    FALLBACK = auto()


class EntryRecord(FrozenModel):
    """How one prompts entry actually played out, for state.json and the structural gates."""

    index: int = Field(description="The entry's position in the case's prompts list")
    kind: TurnEntryKind = Field(description="Which kind of turn source produced the entry's messages")
    exchange_count: int = Field(description="Client messages actually sent for this entry")
    outcome: TurnOutcome = Field(description="Why the entry stopped")
    detail: str = Field(default="", description="The source's reason for stopping; empty when it gave none")
    # Which reply the goal-holding client ruled on, so a reader of the record can find it. The
    # client judges the conversation as it stands, so the satisfying reply is the last one answered
    # when the entry ended -- which is frequently NOT one of this entry's own exchanges: a goal
    # satisfied by the previous entry's reply sends nothing at all and has exchange_count 0. Both
    # keys are empty on every other entry and on a goal entry the client was never satisfied by.
    satisfied_at: str = Field(
        default="", description="UTC ISO 8601 time of the reply that satisfied the client; empty when none did"
    )
    satisfied_at_turn: int | None = Field(
        default=None, description="The 1-based index of that reply's client message; None when none did"
    )


class GoalSatisfactionTiming(FrozenModel):
    """When the goal-holding client said its goal was met, as a span the timing class scores."""

    seconds: float = Field(description="Wall-clock from the case's first client message to the satisfying reply")
    turn_index: int = Field(description="The 1-based client message whose reply satisfied the client")


class TokenBuckets(FrozenModel):
    """Non-overlapping token counts as every usage figure in the trial artifacts spells them.

    ``cache_write`` is the name the artifacts use for what the pricing table calls a cache creation,
    and an unmetered count is a zero rather than an absence, so a reader summing across records never
    meets a null; whether anything metered a turn at all is what its ``message_count`` says.
    """

    input: int = Field(description="Uncached input tokens")
    output: int = Field(description="Output tokens")
    cache_read: int = Field(description="Input tokens served from the prompt cache")
    cache_write: int = Field(description="Input tokens written to the prompt cache")


class TurnRecord(FrozenModel):
    """What one client message cost: its wall-clock, and what the workspace agent consumed answering it.

    Observability only -- no grade-time reader touches these, and nothing here moves a reward.

    A turn earns a record only once its reply has arrived, the way an entry earns one only once it
    has stopped: a turn whose send or whose reply ran out of time leaves none, so a timed-out trial's
    records stop one short of the message it died on.

    Together the records are a floor on what the trial spent rather than a partition of it: the
    welcome turn happens before the first record, a record is taken as soon as the agent reports
    WAITING so the workspace's turn-end flow can spend past it, and a worker's spend is never in
    here at all.

    The reply is noticed by polling, so ``replied_at`` is the first poll that saw it settle rather
    than the instant it did -- late by up to one poll interval (``poll_seconds``, 5 s by default),
    and ``reply_seconds`` overstates by the same margin. Differences smaller than a poll between two
    turns, or between two arms, mean nothing.
    """

    index: int = Field(description="The 1-based index of the client message this turn sent")
    entry_index: int = Field(description="The 0-based prompts entry the turn belongs to")
    exchange: int = Field(description="The 0-based exchange within that entry")
    sent_at: str = Field(description="UTC ISO 8601 time the message reached the workspace")
    replied_at: str = Field(description="UTC ISO 8601 time the reply was first seen complete")
    reply_seconds: float = Field(description="Wall-clock from the send to the complete reply")
    agent_message_count: int = Field(description="Agent messages the reply was made of")
    message_count: int = Field(
        description="Agent messages in this turn's slice of the event stream that carried usage, "
        "which is not agent_message_count: an unmetered turn reports zero here and still has a reply"
    )
    tokens: TokenBuckets = Field(description="Non-overlapping token buckets this turn consumed")
    cost_usd: float | None = Field(
        description="USD for this turn, or None when nothing priced it (an unpriced model, or a "
        "stream carrying no usage at all)"
    )


# What a step name may be: it names a task subdirectory, a harbor step, and a verifier container
# session, so it is restricted to what all three accept.
STEP_NAME_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9-]*$"

# What an upload id may be: it names a directory under the workspace's data/uploads/ and a directory
# in the box, and it is quoted into prompts as a path, so it stays to path-safe characters. Minds
# mints these as bare hex, but an author-written id may be readable as long as it is a plain name.
UPLOAD_ID_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"

# Where a Minds workspace keeps the files its user uploaded. The template ignores this tree, so a
# step's files land untracked, exactly as a real upload does.
WORKSPACE_UPLOADS_DIR: Final[str] = "/home/user/workspace/data/uploads"

# Where a step's uploads wait in the box between harbor putting them there and the driver copying
# them into the workspace. Deliberately outside the box's working directory, which is the mngr
# checkout the workspace's vendored copy is taken from and must stay exactly what the image shipped.
BOX_STEP_FILES_DIR: Final[str] = "/work/step_files"

# Where a step's seeded app waits in the box until the driver copies it into the seed commit, outside
# the box's working directory for the same reason as the uploads.
BOX_STEP_SEEDS_DIR: Final[str] = "/work/step_seeds"

# The workspace template's app-name rule (`validate_service_name` in its
# `system/scripts/forward_port.py`), which a seeded app's name must pass: the name becomes the leading
# label of the app's origin hostname and the name of its supervisord program.
SEED_APP_NAME_PATTERN: Final[str] = r"^[a-z0-9_]+(?:-[a-z0-9_]+)*$"
SEED_APP_NAME_MAX_LENGTH: Final[int] = 32
SEED_APP_RESERVED_NAME_PREFIXES: Final[tuple[str, ...]] = ("host-", "agent-")
SEED_APP_RESERVED_NAMES: Final[frozenset[str]] = frozenset({"localhost", "auth"})

# The loopback ports the workspace template's own services bind, which a seeded app may not take.
SEED_APP_RESERVED_PORTS: Final[frozenset[int]] = frozenset({7681, 7682, 8000, 8010, 8081, 8300, 8301})


# The eval-config key that selects a stepped case's reward strategy, named so the parser and its
# error messages cannot drift from each other.
REWARD_STRATEGY_KEY: Final[str] = "reward_strategy"


class RewardStrategy(LowerCaseStrEnum):
    """How a stepped case's trial reward is derived from its per-step rewards.

    Spelled exactly as harbor's own `multi_step_reward_strategy`, which this is rendered into.
    FINAL scores the trial by the last step that ran, which after an abort is the step that failed;
    MEAN averages every step that produced a reward. Both are legitimate because every step is
    graded by the same verifier on the same scale.
    """

    FINAL = auto()
    MEAN = auto()


class StepFile(FrozenModel):
    """One upload a step introduces into the workspace, exactly as the eval author wrote it."""

    source: str = Field(description="File or directory to ship, relative to the eval config file")
    upload_id: str = Field(description="The directory name it appears under in the workspace's data/uploads/")


class StepBoxFile(FrozenModel):
    """Where one step's upload waits in the box, and the id it takes in the workspace.

    Harbor puts a step's workdir into the box before the step runs, so by the time the driver reads
    this the files are already there; all the driver does is copy them into the running workspace.
    """

    upload_id: str = Field(description="The directory name it takes under the workspace's data/uploads/")
    box_path: str = Field(description="The box directory holding this upload's contents")


class SeedApp(FrozenModel):
    """A fixed app a stepped case's workspace boots with, exactly as the eval author wrote it."""

    source: str = Field(description="The app's directory, relative to the minds_evals project root")
    name: str = Field(description="The app's registry name and supervisord program name")
    port: int = Field(description="The loopback port the app's server binds")


class CaseSeed(FrozenModel):
    """What a stepped case's workspace is seeded with before it is created."""

    app: SeedApp = Field(description="The app the seed adds to the workspace template")


class StepBoxSeedApp(FrozenModel):
    """Where a step's seeded app waits in the box, and the name and port the seed registers it under."""

    name: str = Field(description="The app's registry name and supervisord program name")
    port: int = Field(description="The loopback port the app's server binds")
    box_path: str = Field(description="The box directory holding the app's files")


class RewardDimension(LowerCaseStrEnum):
    """A key of the verifier's reward.json that a step's `min_reward` may gate on.

    Every member but REWARD is a dimension rewardkit scores; REWARD is the composed, gated score
    finalize.py writes, and is what harbor compares a bare numeric `min_reward` against.
    """

    GATES = auto()
    QUALITY = auto()
    HARNESS_QUALITY = auto()
    OUTCOME = auto()
    REWARD = auto()


class RewardFloor(FrozenModel):
    """One dimension's threshold within a step's `min_reward` mapping."""

    dimension: RewardDimension = Field(description="Which reward.json key this threshold applies to")
    floor: float = Field(description="The value that key must reach for the trial to go on")


class ComposedRewardFloor(FrozenModel):
    """A step's `min_reward` authored as a bare number: a floor on the composed `reward` key."""

    floor: float = Field(description="The value the composed reward must reach for the trial to go on")


class PerDimensionRewardFloors(FrozenModel):
    """A step's `min_reward` authored as a mapping: one floor per named reward dimension.

    A dimension the mapping leaves out is not gated at all, since harbor reads a missing key as
    -inf. At least one floor is required: an empty mapping renders as `min_reward = { }`, which
    harbor reads as a gate that can never fail, so the step would declare a threshold it does not
    have and the trial would run to the end looking fine.
    """

    floors: tuple[RewardFloor, ...] = Field(
        min_length=1, description="The per-dimension floors, in the order the config named them"
    )


# The reward a step must reach for the trial to continue, in the two forms harbor accepts. They are
# separate types rather than one model with two optional fields so that "exactly one of them" is a
# property of the type: neither and both are the shapes that render TOML saying something the eval
# config did not.
StepMinReward = ComposedRewardFloor | PerDimensionRewardFloors


class CaseStep(FrozenModel):
    """One named stretch of a stepped case: its own turns, the uploads it introduces, what it is
    graded against, and the reward it must reach for the trial to go on."""

    name: str = Field(description="Stable step name; names the step directory and the harbor step")
    prompts: tuple[PromptEntry, ...] = Field(description="One entry per turn, exactly as a flat case's prompts")
    files: tuple[StepFile, ...] = Field(description="What the client 'uploaded' for this step")
    seed: CaseSeed | None = Field(description="What the workspace is seeded with; only ever set on the first step")
    expectations: Expectations | None = Field(
        description="What this step is graded against; None grades it on gates and quality alone"
    )
    min_reward: StepMinReward | None = Field(
        description="The reward floor below which harbor aborts the remaining steps; None never aborts"
    )
    is_diagnostic_probe_run: bool = Field(
        description="Whether the step's collection runs the self-diagnostic probe and reads the feed's tool inputs"
    )


class StepPosition(FrozenModel):
    """Where one instruction sits in its multi-step task.

    Carried in the per-step case config because a harbor agent is invoked once per step with only
    that step's instruction, and the driver has to know which invocation is the last one: the
    evidence phase runs at the end of every step, but the workspace may only be destroyed once no
    later step can still use it.
    """

    name: str = Field(description="The step's name")
    index: int = Field(description="The step's 0-based position in the task's step list")
    total: int = Field(description="How many steps the task declares")
    # `timeout_seconds` on the config beside this one is only THIS step's share. Anything that has to
    # outlive the step it was started in -- the reverse tunnel the workspace reaches the proxy on --
    # must be sized against this instead, or it closes under a later step. It is not the sum of the
    # steps' conversation shares: between two conversations the trial also spends a step's evidence
    # phase, its cleanup grace and its verifier container, and the tunnel has to span all of that.
    trial_lifetime_seconds: float = Field(
        description="How long something started on the first step must live to still serve the last"
    )
    # The conversation and its per-entry records are cumulative, while the config beside this one
    # holds only this step's turns, so the structural gates need this to know how many entries the
    # trial owes by the end of this step.
    entries_before: int = Field(description="How many prompts entries the earlier steps configured")
    files: tuple[StepBoxFile, ...] = Field(description="This step's uploads, and where the box holds them")
    is_diagnostic_probe_run: bool = Field(
        description="Whether this step's collection runs the self-diagnostic probe and the driver reads the "
        "feed's tool inputs"
    )
    seed_app: StepBoxSeedApp | None = Field(
        description="The app the workspace is seeded with, on the first step of a seeded case; None otherwise"
    )


@pure
def is_final_step(step: StepPosition | None) -> bool:
    """Whether this instruction is the last one the trial will run. A case with no steps is a
    single-step task, whose one run() is by definition the last."""
    return step is None or step.index == step.total - 1


class PersonaCase(FrozenModel):
    """One persona case from an eval config: an id, an optional persona, and its prompts entries."""

    case_id: str = Field(description="Stable case id; names the task directory and the trial")
    persona: str = Field(description="Client persona role-played on DECIDE_FROM_PERSONA turns (may be empty)")
    # The flat view of the whole case, whether or not it declares steps: the oracle, the timeout
    # warning, and the verifier's structural gates all reason about the case's turns as one list.
    prompts: tuple[PromptEntry, ...] = Field(
        description="The conversation's entries in order: a literal message, the sentinel, or a goal"
    )
    steps: tuple[CaseStep, ...] | None = Field(
        description="The named steps whose prompts `prompts` flattens; None for a single-step case"
    )
    # None for a stepped case, where every step states its own instead, so that a reader of a step's
    # instruction sees exactly what that step is graded on.
    expectations: Expectations | None = Field(description="What the delivered artifact must be, if the case says")
    reward_strategy: RewardStrategy = Field(description="How a stepped case's per-step rewards become the trial's")


class EvalConfig(FrozenModel):
    """A validated eval config file: the mngr branch under test plus the persona cases."""

    mngr_branch: str = Field(description="The mngr ref (branch, tag, or full SHA) the box is built from")
    dwt_repo: str = Field(description="Workspace template repo each case is cloned from")
    dwt_branch: str = Field(description="Workspace template ref (branch, tag, or full SHA)")
    timeout_seconds: float = Field(description="Per-case wall-clock budget in seconds")
    verification_timeout_seconds: float = Field(description="Wall-clock budget for the evidence-collection phase")
    cases: tuple[PersonaCase, ...] = Field(description="The persona cases, one task each")


class CaseConfig(FrozenModel):
    """The full per-case config carried in the task instruction and in tests/case.json."""

    case_id: str = Field(description="Stable case id")
    persona: str = Field(description="Client persona for DECIDE_FROM_PERSONA turns (may be empty)")
    prompts: tuple[PromptEntry, ...] = Field(description="The conversation's entries in order")
    timeout_seconds: float = Field(description="Per-case wall-clock budget in seconds")
    verification_timeout_seconds: float = Field(description="Wall-clock budget for the evidence-collection phase")
    mngr_branch: str = Field(description="The mngr ref (branch, tag, or full SHA) the box was built from")
    mngr_sha: str = Field(description="Exact mngr SHA resolved at generation time")
    dwt_repo: str = Field(description="Workspace template repo")
    dwt_branch: str = Field(description="Workspace template ref the SHA was resolved from")
    dwt_sha: str = Field(description="Exact workspace template SHA resolved at generation time")
    # The expanded form is what both the collector and the verifier act on; the authored form rides
    # along so a reader of instruction.md or case.json can see what the config actually said.
    expectations: ExpandedExpectations | None = Field(description="The expanded expectations, if the case has any")
    authored_expectations: Expectations | None = Field(description="The expectations exactly as authored")
    # None in the task-level tests/case.json (which describes the whole case) and in every
    # single-step task; set only in a step's own instruction, where `prompts` holds that step's turns
    # rather than the case's.
    step: StepPosition | None = Field(description="Which step of a multi-step task this config drives")


@pure
def cross_step_lifetime_seconds(case: CaseConfig) -> float:
    """How long something started on the first step must live to still serve the last one, whichever
    step's config is in hand. A single-step case's is just its conversation budget, since nothing
    outlives the one run() call."""
    return case.timeout_seconds if case.step is None else case.step.trial_lifetime_seconds


class Transcript(FrozenModel):
    """The conversation so far, as raw chat app events (verbatim schema)."""

    events: tuple[dict[str, Any], ...] = Field(description="Raw events from the workspace's chat app")


class DeciderResult(FrozenModel):
    """One decider (simulated-user) model call: the message plus usage accounting."""

    message: str = Field(description="The client's next message")
    model: str = Field(description="The decider model the call was made against; empty when there was no call")
    # A fallback does not imply zeros: a call that came back with an answer the client could not act
    # on was still billed for answering, and this is the only place that cost is ever measured.
    input_token_count: int = Field(description="Input tokens the call consumed; 0 when none completed")
    output_token_count: int = Field(description="Output tokens the call consumed; 0 when none completed")
    is_fallback: bool = Field(description="Whether the literal fallback message was used")


class JudgeScore(FrozenModel):
    """One likert judge criterion's score on a trial. Reported for the record, never gated."""

    dimension: str = Field(description="The rewardkit dimension the criterion was scored under")
    criterion: str = Field(description="The judge criterion's name, e.g. 'conciseness'")
    # rewardkit normalizes a 1-10 likert to (raw - 1) / 9; both are carried so a reader can compare
    # across runs without having to know which convention a number is in.
    normalized_score: float = Field(description="The criterion's contribution to its dimension, 0-1")
    raw_score: float = Field(description="The judge's own 1-10 likert answer")


class TrialCheck(FrozenModel):
    """How one trial of a finished run came out, under the criteria a scheduled run is gated on."""

    trial_name: str = Field(description="The trial directory's name, e.g. 'todo-app__XNXFsgk'")
    case_id: str = Field(description="The persona case the trial ran; empty when it cannot be read")
    is_completed: bool = Field(description="Whether the trial ran to the end without erroring or timing out")
    incompletion_reason: str = Field(description="Why the trial did not complete; empty when it did")
    is_gates_passed: bool = Field(description="Whether every structural gate criterion scored above zero")
    error_entry_ids: tuple[str, ...] = Field(description="Evidence manifest entries the harness could not measure")
    reward: float | None = Field(description="The trial's final reward; None when it was never graded")
    judge_scores: tuple[JudgeScore, ...] = Field(description="Every likert judge criterion the verifier recorded")
    lane: str = Field(description="The provider lane the trial signed in on; empty when it recorded none")
    requested_model: str = Field(
        description="The catalog id the trial asked the chat to run on; empty when it asked for none"
    )
    is_model_confirmed: bool | None = Field(
        description="Whether the models observed were exactly the requested one; None wherever it could not be told"
    )
    wrong_model_reason: str = Field(
        description="Why the trial ran on another model than it asked for; empty when it did not"
    )
    modal_environment_name: str = Field(description="The Modal environment the trial left behind; empty if unrecorded")
    mngr_sha: str = Field(description="The mngr SHA the trial's box was built from")
    dwt_sha: str = Field(description="The workspace-template SHA the trial's workspace was cloned from")

    @computed_field
    @cached_property
    def is_passed(self) -> bool:
        return self.is_completed and self.is_gates_passed and not self.error_entry_ids and not self.wrong_model_reason


class RunCheck(FrozenModel):
    """Whether a finished job passed the criteria a scheduled run is gated on, trial by trial."""

    job_name: str = Field(description="The job directory's name")
    trials: tuple[TrialCheck, ...] = Field(description="One entry per trial directory, in name order")

    @computed_field
    @cached_property
    def is_passed(self) -> bool:
        """Whether every trial passed; the check-run command's exit code follows this.

        A run with no trials is not a pass: it says nothing ran, which must never read as a green
        verdict.
        """
        return bool(self.trials) and all(trial.is_passed for trial in self.trials)

    @computed_field
    @cached_property
    def modal_environment_names(self) -> tuple[str, ...]:
        """Every environment the run leaked, so the same file that reports the run names what a
        cleanup pass has to remove.

        Derived from the rows rather than stored beside them, like the verdict above: a list a
        caller had to keep in step with `trials` is one that can name an environment no trial in
        the report created. A trial that never reached a workspace records no name, and an empty
        one is dropped rather than reported as an environment to go looking for.
        """
        return tuple(sorted({trial.modal_environment_name for trial in self.trials if trial.modal_environment_name}))


class NotRecorded(FrozenModel):
    """The one value that is not a reading: the record a fact reads was due on a step that ran and is
    absent or unreadable.

    Distinct from a recorded null, which is a record that is there and cannot answer, and from the
    fact being absent from a block altogether, which is a record that was never due. `NOT_RECORDED`
    is the singleton; it never reaches JSON, where a step's block carries its not-recorded facts as
    a list of names beside the values.
    """


NOT_RECORDED: Final[NotRecorded] = NotRecorded()

# What one fact of one step reads: a JSON value, or the not-recorded sentinel above.
FactValue = JsonValue | NotRecorded

# How a table names one fact of one step: `<fact name>` for the last step that ran, or
# `<fact name>@<step name>` to pin it to one step.
FACT_STEP_SEPARATOR: Final[str] = "@"


@pure
def split_fact_reference(reference: str) -> tuple[str, str]:
    """A table key as (fact name, step name); the step name is empty when the key pins no step.

    Raises ExpectedFactsTableError for a key that names a step twice or names nothing.
    """
    fact_name, separator, step_name = reference.partition(FACT_STEP_SEPARATOR)
    if FACT_STEP_SEPARATOR in step_name:
        raise ExpectedFactsTableError("{!r} names more than one step".format(reference))
    if not fact_name or (separator and not step_name):
        raise ExpectedFactsTableError("{!r} is not a <fact> or <fact>@<step> reference".format(reference))
    return fact_name, step_name


class FactMatcherKind(LowerCaseStrEnum):
    """How an expected-facts table compares a recorded fact value with the value it states."""

    EXPECTED = auto()
    AT_LEAST = auto()
    AT_MOST = auto()
    CONTAINS = auto()


class FactMatcher(FrozenModel):
    """One comparison a table states for a fact: the kind of comparison and the value compared against."""

    kind: FactMatcherKind = Field(description="Which comparison is made")
    value: JsonValue = Field(description="The exact value, the bound, or what the recorded value must contain")


class KnownFailure(FrozenModel):
    """A known defect: the value the fact's matcher states is the one the defect records today.

    Every known failure is strict, so a trial that stops recording the defect is a failure that says
    the mark must come off. It names either the issue that tracks the defect or, for one no issue
    tracks, the reason in prose.
    """

    issue: int | None = Field(default=None, gt=0, description="The GitHub issue that tracks the defect")
    reason: str = Field(default="", description="Why the fact fails today, for a defect no issue tracks")

    @model_validator(mode="after")
    def _validate_one_name(self) -> Self:
        if (self.issue is None) == (not self.reason):
            raise ExpectedFactsTableError("a known failure names an issue or a reason, not both and not neither")
        return self

    @property
    def label(self) -> str:
        """How a report names the defect."""
        return "#{}".format(self.issue) if self.issue is not None else self.reason


class _FactMatcherFields(FrozenModel):
    """The four matcher keys a table entry or an override may carry, at most one of them."""

    expected: JsonValue = Field(default=None, description="The exact value; null matches only a recorded null")
    at_least: StrictInt | StrictFloat | None = Field(default=None, description="The smallest number that matches")
    at_most: StrictInt | StrictFloat | None = Field(default=None, description="The largest number that matches")
    contains: JsonValue = Field(default=None, description="A member of a recorded list, or a substring of a string")

    def declared_matcher(self) -> FactMatcher | None:
        """The matcher the entry states, or None when it states none. Read off which keys were given
        rather than off their values, because `expected: null` is a matcher."""
        kinds = [kind for kind in FactMatcherKind if kind.value in self.model_fields_set]
        if not kinds:
            return None
        match kinds[0]:
            case FactMatcherKind.EXPECTED:
                return FactMatcher(kind=FactMatcherKind.EXPECTED, value=self.expected)
            case FactMatcherKind.AT_LEAST:
                return FactMatcher(kind=FactMatcherKind.AT_LEAST, value=self.at_least)
            case FactMatcherKind.AT_MOST:
                return FactMatcher(kind=FactMatcherKind.AT_MOST, value=self.at_most)
            case FactMatcherKind.CONTAINS:
                return FactMatcher(kind=FactMatcherKind.CONTAINS, value=self.contains)
            case _ as unreachable:
                assert_never(unreachable)

    @property
    def is_unobservable_declared(self) -> bool:
        """Whether this entry states `expected: null`, which claims the fact cannot be observed."""
        return FactMatcherKind.EXPECTED.value in self.model_fields_set and self.expected is None

    @model_validator(mode="after")
    def _validate_at_most_one_matcher(self) -> Self:
        given_keys = [kind.value for kind in FactMatcherKind if kind.value in self.model_fields_set]
        if len(given_keys) > 1:
            raise ExpectedFactsTableError("give one matcher, not {}".format(" and ".join(given_keys)))
        # A bound of null would compare nothing, so it is refused rather than read as no bound.
        for bound_key, bound in (
            (FactMatcherKind.AT_LEAST.value, self.at_least),
            (FactMatcherKind.AT_MOST.value, self.at_most),
        ):
            if bound_key in self.model_fields_set and bound is None:
                raise ExpectedFactsTableError("{} needs a number, not null".format(bound_key))
        return self


class FactOverride(_FactMatcherFields):
    """What one harness changes about a fact's expectation: its matcher, its known failure, or both.
    Whatever it leaves out is the fact's own."""

    known_failure: KnownFailure | None = Field(default=None, description="The known failure this override declares")


class FactExpectation(_FactMatcherFields):
    """One fact's entry in an expected-facts table, under the key that names the fact and its step."""

    by_harness: dict[str, FactOverride] = Field(
        default_factory=dict, description="Overrides for a trial on one harness, by harness name"
    )
    known_failure: KnownFailure | None = Field(
        default=None, description="The defect whose recorded value the fact's matcher states"
    )
    is_compliance: bool = Field(
        default=False,
        alias="compliance",
        description="Whether the fact says the agent did what the prompt asked, rather than how the instrument read it",
    )
    is_health: bool = Field(
        default=False,
        alias="health",
        description="Whether the fact says a compliance source could be read at all",
    )
    requires: tuple[str, ...] = Field(
        default=(), description="The compliance and health facts that must pass for this fact to be asserted at all"
    )

    @property
    def is_agent_reading(self) -> bool:
        """Whether the fact reads the agent's compliance or the health of a source that reads it.

        Both are the same thing to the checker in one respect: an instrument that could not read one
        is broken, so a null or unrecorded value fails rather than reading as the agent's doing.
        """
        return self.is_compliance or self.is_health

    @model_validator(mode="after")
    def _validate_expectation(self) -> Self:
        if self.declared_matcher() is None:
            raise ExpectedFactsTableError(
                "give exactly one of {}".format(", ".join(kind.value for kind in FactMatcherKind))
            )
        if self.is_compliance and self.is_health:
            raise ExpectedFactsTableError("a fact is a compliance fact or a health fact, not both")
        overrides = list(self.by_harness.values())
        if self.is_agent_reading and (
            self.known_failure is not None or any(override.known_failure is not None for override in overrides)
        ):
            # A compliance miss is the agent's and a health miss is the instrument's, so neither has
            # an instrument defect to be known as.
            raise ExpectedFactsTableError("a compliance or health fact cannot declare a known failure")
        # A fact declared unobservable is a defect with a name, never a permanent exemption. An
        # override inherits the fact's own mark, so only a mark on neither is a refusal.
        if self.is_unobservable_declared and self.known_failure is None:
            raise ExpectedFactsTableError("`expected: null` needs a known_failure mark")
        for override in overrides:
            if override.is_unobservable_declared and override.known_failure is None and self.known_failure is None:
                raise ExpectedFactsTableError("`expected: null` needs a known_failure mark")
        return self


class ExpectedFactsTable(FrozenModel):
    """A self-diagnostic family's expectations: which facts a trial is asserted on, and how."""

    case_id: str = Field(
        default="", description="The case the table grades; empty applies it to any case, as live invariants do"
    )
    requires: tuple[str, ...] = Field(
        default=(), description="Facts that must pass for any fact of this table to be asserted"
    )
    facts: dict[str, FactExpectation] = Field(
        description="Every asserted fact's expectation, by `<fact>` or `<fact>@<step>` reference"
    )

    @model_validator(mode="after")
    def _validate_fact_references(self) -> Self:
        for reference in (*self.facts, *self.requires):
            split_fact_reference(reference)
        agent_reading_references = {
            reference for reference, expectation in self.facts.items() if expectation.is_agent_reading
        }
        for reference in self.requires:
            if reference not in self.facts:
                raise ExpectedFactsTableError("the table requires {}, which it does not assert".format(reference))
        for reference, expectation in self.facts.items():
            for required in expectation.requires:
                if required not in agent_reading_references:
                    raise ExpectedFactsTableError(
                        "{} requires {}, which is not a compliance or health fact of this table".format(
                            reference, required
                        )
                    )
        _validate_no_requirement_cycle(self.facts, self.requires)
        return self


@pure
def fact_requirements(
    facts: Mapping[str, FactExpectation], table_requires: Sequence[str], reference: str
) -> tuple[str, ...]:
    """What one fact of a table waits on: its own requirements, and the table's unless it is one of
    them -- two facts the whole table requires would otherwise wait on each other."""
    inherited = () if reference in table_requires else tuple(table_requires)
    return (*inherited, *facts[reference].requires)


@pure
def _walked_requirements(
    facts: Mapping[str, FactExpectation], table_requires: Sequence[str], reference: str, path: tuple[str, ...]
) -> frozenset[str]:
    """The references reachable from `reference`, proven acyclic.

    Raises ExpectedFactsTableError when it requires its way back to something on `path`.
    """
    if reference in path:
        raise ExpectedFactsTableError("the requirements form a cycle: {}".format(" -> ".join((*path, reference))))
    reached = frozenset({reference})
    for required in fact_requirements(facts, table_requires, reference):
        reached |= _walked_requirements(facts, table_requires, required, (*path, reference))
    return reached


@pure
def _validate_no_requirement_cycle(facts: Mapping[str, FactExpectation], table_requires: Sequence[str]) -> None:
    """Raises ExpectedFactsTableError when the requirements name each other in a loop, which would
    leave every fact of the loop waiting on the others.

    Walked over the same edges the checker resolves the outcomes in, the table-level requirement
    included, so a loop that only forms through it is refused here rather than met at check time.
    """
    for reference in facts:
        _walked_requirements(facts, table_requires, reference, ())


class DiagnosticVerdict(LowerCaseStrEnum):
    """What one self-diagnostic trial says about the instrument. Only FAILED is the instrument's fault."""

    NOT_MEASURED = auto()
    FAILED = auto()
    NOT_FOLLOWED = auto()
    KNOWN = auto()
    PASSED = auto()


class FactStatus(LowerCaseStrEnum):
    """How one asserted fact came out on one trial."""

    PASSED = auto()
    FAILED = auto()
    NOT_FOLLOWED = auto()
    PRECONDITION_NOT_MET = auto()
    # The record the fact reads was due and is absent or unreadable, whatever the table expected.
    NOT_RECORDED = auto()
    # A known failure whose defect this trial recorded.
    KNOWN = auto()
    # A known failure whose defect this trial did not record, so the mark must come off.
    UNEXPECTEDLY_PASSING = auto()


class FactOutcome(FrozenModel):
    """One asserted fact on one trial: what was recorded, what the table expected, and how that came out."""

    fact_name: str = Field(description="The dotted fact name")
    step_name: str = Field(description="The step whose block the fact was read from; empty for a flat trial")
    status: FactStatus = Field(description="How the fact came out")
    is_compliance: bool = Field(description="Whether the fact is a compliance fact")
    is_health: bool = Field(description="Whether the fact is a health fact")
    recorded: JsonValue = Field(description="The recorded value; null where the step recorded none")
    expected: FactMatcher = Field(description="The matcher the table applied, after the trial's overrides")
    known_failure: KnownFailure | None = Field(description="The known failure the table applied, if any")
    unmet_requirements: tuple[str, ...] = Field(description="The required compliance and health facts that missed")


class DiagnosticTrialCheck(FrozenModel):
    """How one trial of a self-diagnostic job came out against its family's expected table."""

    trial_name: str = Field(description="The trial directory's name")
    case_id: str = Field(description="The case the trial ran; empty when it cannot be read")
    harness: str = Field(description="The harness the trial's arm records; empty when it records none")
    verdict: DiagnosticVerdict = Field(description="The trial's verdict")
    not_measured_reason: str = Field(description="Why no workspace was measured; empty unless the verdict says so")
    failed_facts: tuple[FactOutcome, ...] = Field(description="Instrument and health facts that missed")
    not_recorded_facts: tuple[FactOutcome, ...] = Field(
        description="Asserted facts whose record was due and is absent or unreadable"
    )
    known_facts: tuple[FactOutcome, ...] = Field(description="Known failures whose defect was recorded")
    not_followed_facts: tuple[FactOutcome, ...] = Field(description="Compliance facts that read false")
    unmet_preconditions: tuple[FactOutcome, ...] = Field(
        description="Facts not asserted because a fact they require missed"
    )
    unexpectedly_passing_facts: tuple[FactOutcome, ...] = Field(
        description="Known failures whose defect was not recorded"
    )


class DiagnosticRunCheck(FrozenModel):
    """Every trial of a self-diagnostic job, checked against one expected table."""

    job_name: str = Field(description="The job directory's name")
    trials: tuple[DiagnosticTrialCheck, ...] = Field(description="One entry per trial directory, in name order")

    @computed_field
    @cached_property
    def is_failed(self) -> bool:
        """Whether any trial's verdict is failed; the check-diagnostics exit code follows this alone."""
        return any(trial.verdict is DiagnosticVerdict.FAILED for trial in self.trials)


class ModalDeletionOutcome(UpperCaseStrEnum):
    """How one Modal environment deletion came out.

    DELETED and NOT_FOUND both mean the environment is gone, whether or not this call is what removed
    it; only FAILED is a reason to look."""

    DELETED = auto()
    NOT_FOUND = auto()
    FAILED = auto()


class CleanupReport(FrozenModel):
    """What one Modal environment cleanup pass did."""

    deleted_names: tuple[str, ...] = Field(description="Environments the pass removed")
    already_gone_names: tuple[str, ...] = Field(description="Environments that were absent by the time it looked")
    failed_names: tuple[str, ...] = Field(description="Environments Modal refused to remove")


class HarnessConfigEntry(FrozenModel):
    """One named harness config in `configs/harness_configs.json`: the kwargs a scheduled cell
    appends to its run line, under a name that labels the cell wherever it is reported.

    The kwarg fields are the run line's own (`--ak lane=`, `--ak model=`, ...) and carry the same
    defaults an empty kwarg does, so an entry that names nothing but its lane is the default harness
    config: the product exactly as it ships. Validation is the driver's `parse_harness_config`, not
    anything here; this model only holds the file's shape.
    """

    name: str = Field(description="The config's name, as job names, artifact names and the Slack report spell it")
    is_nightly: bool = Field(description="Whether a scheduled run (and a dispatch naming no configs) runs it")
    lane: str = Field(default="", description="The provider lane to sign in on; empty means anthropic")
    key_provider: str = Field(default="", description="Which provider the key belongs to, on the api-key lane only")
    key_env: str = Field(default="", description="The variable holding the lane's key; empty derives it from the lane")
    model: str = Field(default="", description="The catalog id to switch the chat to before turn 1; empty means none")
    effort: str = Field(default="", description="The effort level set with the model; empty means none")
    fast: bool | None = Field(default=None, description="The speed tier set with the model; None leaves it unset")


class HarnessConfigsFile(FrozenModel):
    """The shape of `configs/harness_configs.json`."""

    harness_configs: tuple[HarnessConfigEntry, ...] = Field(description="Every named config, nightly or not")


class DiagnosticHarnessEntry(FrozenModel):
    """What a self-diagnostic family runs on, as `configs/diagnostics/` spells it: the `--ak` values a
    diagnose job appends to its run line, each the text an operator would type.

    The fields are `HarnessConfigEntry`'s kwargs, minus the name and the nightly flag: a family's file
    is keyed by family or harness, and a diagnostic runs every night whatever the green markers say.
    Validation is the driver's `parse_harness_config`, not anything here; this model only holds the
    file's shape.
    """

    lane: str = Field(default="", description="The provider lane to sign in on; empty means anthropic")
    key_provider: str = Field(default="", description="Which provider the key belongs to, on the api-key lane only")
    key_env: str = Field(default="", description="The variable holding the lane's key; empty derives it from the lane")
    model: str = Field(default="", description="The catalog id to switch the chat to before turn 1; empty means none")
    effort: str = Field(default="", description="The effort level set with the model; empty means none")
    fast: str = Field(
        default="", description="The speed tier set with the model, as a boolean kwarg; empty means unset"
    )


class BehaviourHarnessEntry(DiagnosticHarnessEntry):
    """One harness's behaviour cell, as `configs/diagnostics/behaviour_harness_configs.json` spells it."""

    unsupported_pairs: tuple[str, ...] = Field(
        default=(),
        description="Pairs whose workspace template offers this lane no pasted-key sign-in, so the cell is left out",
    )


class NightlySuiteArm(FrozenModel):
    """One arm of a nightly suite: a harness config named by the one file that defines it.

    The name is the whole of an arm's identity here. What the arm drives -- its lane, its key, its
    model -- lives in `configs/harness_configs.json`, so a suite can only name arms that file has
    already validated.
    """

    name: str = Field(description="The harness config's name, as `configs/harness_configs.json` spells it")
    attempts: PositiveInt = Field(
        default=PositiveInt(1), description="How many times a cell runs each case: harbor's `-k`"
    )


class NightlySuite(FrozenModel):
    """One eval config a scheduled run evaluates, and the arms it runs it on.

    An empty arm list means every entry the harness configs file marks nightly, which is the set a
    config worth running on everything wants. A named list runs exactly those arms whatever their
    `is_nightly` flag says, so an arm too expensive to run against every config can still be named
    by the one suite that wants it.
    """

    config: str = Field(description="The repo-relative eval config the suite's cells generate their dataset from")
    harness_configs: tuple[NightlySuiteArm, ...] = Field(
        default=(),
        description="The arms to run it on; empty means every entry the harness configs file marks nightly",
    )


class NightlySuitesFile(FrozenModel):
    """The shape of `configs/nightly_suites.json`."""

    suites: tuple[NightlySuite, ...] = Field(description="Every suite a scheduled run evaluates, in order")


class SuiteArm(FrozenModel):
    """One arm of a suite with its name looked up: the harness config the file holds under it, and
    how many times the cell runs each case."""

    entry: HarnessConfigEntry = Field(description="The named harness config, as the harness configs file holds it")
    attempts: PositiveInt = Field(description="How many times the cell runs each case: harbor's `-k`")


class ResolvedSuite(FrozenModel):
    """A suite every cell of the run can be composed from: its config, the one word every job and
    artifact name spells that config as, and its arms in the order their cells are decided in."""

    config: str = Field(description="The repo-relative eval config the suite's cells generate their dataset from")
    config_slug: str = Field(description="The config's one-word name, which job and artifact names carry")
    arms: tuple[SuiteArm, ...] = Field(description="The arms the suite runs, in decision order")


class PairDecision(LowerCaseStrEnum):
    """What a scheduled run decided about one (mngr, dwt) pair.

    UNRESOLVED means one of its refs is not on its remote, so it has no SHAs and no cells; SKIP means
    every one of its cells is already green; RUN means at least one cell runs, and with it the oracle
    pass of every eval config that cell's suite named.
    """

    RUN = auto()
    SKIP = auto()
    UNRESOLVED = auto()


class CellDecision(LowerCaseStrEnum):
    """What a scheduled run decided about one cell: run it, or skip it because its green marker
    says this exact arm was already verified."""

    RUN = auto()
    SKIP = auto()


class FrozenPair(FrozenModel):
    """A (mngr, dwt) pair as the scheduled run's resolve job froze it: each ref resolved to a SHA
    once, so nothing can drift between dataset generation and the run.

    An unresolvable pair keeps its refs and carries empty SHAs; that is how the freeze reports it.
    """

    pair: str = Field(description="The pair's name: main, released, or custom")
    mngr_ref: str = Field(description="The mngr ref the pair was asked for")
    mngr_sha: str = Field(description="The mngr SHA it resolved to; empty when it did not resolve")
    dwt_ref: str = Field(description="The workspace-template ref the pair was asked for")
    dwt_sha: str = Field(description="The workspace-template SHA it resolved to; empty when it did not resolve")

    @property
    def is_resolved(self) -> bool:
        return bool(self.mngr_sha) and bool(self.dwt_sha)


class DecidedPair(FrozenPair):
    """A frozen pair with the run's decision about it."""

    decision: PairDecision = Field(description="Whether the pair runs, is skipped, or never resolved")


class MatrixCell(FrozenModel):
    """One arm of a scheduled run: a frozen pair times one eval config times one named harness
    config, with everything the cell's job needs spelled out so the workflow reads values and
    composes nothing.

    Every field is a scalar on purpose: the cell is a GitHub Actions matrix entry, which carries no
    lists, so `harbor_args` rides as JSON-encoded text.
    """

    pair: str = Field(description="The pair's name")
    harness_config: str = Field(description="The harness config's name")
    mngr_ref: str = Field(description="The mngr ref the pair was asked for")
    mngr_sha: str = Field(description="The mngr SHA the pair froze to")
    dwt_ref: str = Field(description="The workspace-template ref the pair was asked for")
    dwt_sha: str = Field(description="The workspace-template SHA the pair froze to")
    config: str = Field(description="The repo-relative eval config the dataset is generated from")
    config_slug: str = Field(description="The config's one-word name, which job, artifact and summary names carry")
    attempts: PositiveInt = Field(description="How many times harbor runs each case: its `-k`, passed only above 1")
    lane_key_env: str = Field(description="The environment variable the driver reads the lane's key from")
    harbor_args: str = Field(description="JSON-encoded list of the `--ak` arguments appended to the run line")
    cache_key: str = Field(
        description="The green marker's cache key: pair, both SHAs, config, harness config and its kwarg digest"
    )
    decision: CellDecision = Field(description="Whether the cell runs or its green marker already holds")


class FixtureDiagnosticCell(FrozenPair):
    """One resolved pair's `diagnose-fixture` job: the pair, and the run line its fixture trial takes.

    A GitHub Actions matrix entry, so every field is a string and `harbor_args` is JSON-encoded, as a
    live cell's is. It carries no decision: a diagnostic writes no green marker and reads none.
    """

    lane_key_env: str = Field(description="The environment variable the driver reads the lane's key from")
    harbor_args: str = Field(description="JSON-encoded list of the `--ak` arguments appended to the run line")


class BehaviourDiagnosticCell(FixtureDiagnosticCell):
    """One `diagnose-behaviour` job: a resolved pair times one harness of the nightly set."""

    harness: str = Field(description="The harness the cell measures, as the workspace spells it (`pi-coding`)")


class UnsupportedDiagnosticCell(FrozenModel):
    """A behaviour cell the run does not schedule, because its harness config says the pair cannot run it.

    No box is spent to produce a dark cell, so the report names it instead: a night that measured one
    harness less must not read as a night that measured them all.
    """

    pair: str = Field(description="The pair the cell would have run on")
    harness: str = Field(description="The harness the cell would have measured")
    reason: str = Field(description="Why it cannot run there, in the words the report prints")


class OraclePass(FrozenPair):
    """One oracle pass of a scheduled run: a frozen pair times one eval config.

    The oracle boots no workspace, so it is the same whatever harness config a cell runs on -- but
    it does build the box image, generate the task, run the verifier container and grade what comes
    back, and all four are the eval config's own. So there is one pass per (pair, config), and a
    cell gates on the one its own config named rather than on its pair's.
    """

    config: str = Field(description="The repo-relative eval config it generates its dataset from")
    config_slug: str = Field(description="The config's one-word name, which its job and artifact names carry")


class CiMatrix(FrozenModel):
    """What the scheduled run's resolve job decided, for the jobs after it: the pairs and cells it
    considered, the diagnose jobs of every resolved pair, and the job matrices (oracle passes
    per running pair and config, live passes per running cell) read straight out of the
    serialized form."""

    configs: tuple[str, ...] = Field(description="The repo-relative eval configs the run's suites named, in order")
    pairs: tuple[DecidedPair, ...] = Field(description="Every pair considered, skipped and unresolved ones included")
    cells: tuple[MatrixCell, ...] = Field(description="Every cell of every resolved pair, skipped ones included")
    fixture_diagnostics: tuple[FixtureDiagnosticCell, ...] = Field(
        description="One fixture diagnostic per resolved pair, whatever its cells decided"
    )
    behaviour_diagnostics: tuple[BehaviourDiagnosticCell, ...] = Field(
        description="One behaviour diagnostic per resolved pair and supported nightly harness"
    )
    unsupported_diagnostics: tuple[UnsupportedDiagnosticCell, ...] = Field(
        description="Every behaviour cell left out because its harness config cannot run on that pair"
    )

    @computed_field
    @cached_property
    def oracle_matrix(self) -> dict[str, list[dict[str, Any]]]:
        """The `oracle` job's matrix: one entry per (pair, eval config) that has a cell to run.

        Grouped off the running cells themselves, so a pass exists exactly where a cell gates on
        one: an oracle nothing waits for is money spent on nothing, and a cell whose gate was never
        run cannot start at all.
        """
        passes: dict[tuple[str, str], OraclePass] = {}
        for cell in self.cells:
            if cell.decision is not CellDecision.RUN:
                continue
            passes.setdefault(
                (cell.pair, cell.config),
                OraclePass(
                    pair=cell.pair,
                    mngr_ref=cell.mngr_ref,
                    mngr_sha=cell.mngr_sha,
                    dwt_ref=cell.dwt_ref,
                    dwt_sha=cell.dwt_sha,
                    config=cell.config,
                    config_slug=cell.config_slug,
                ),
            )
        return {"include": [oracle_pass.model_dump(mode="json") for oracle_pass in passes.values()]}

    @computed_field
    @cached_property
    def matrix(self) -> dict[str, list[dict[str, Any]]]:
        """The `evaluate` job's matrix: one entry per running cell."""
        return {
            "include": [
                cell.model_dump(mode="json", exclude={"decision"})
                for cell in self.cells
                if cell.decision is CellDecision.RUN
            ]
        }

    @computed_field
    @cached_property
    def is_any_cell_running(self) -> bool:
        return any(cell.decision is CellDecision.RUN for cell in self.cells)

    @computed_field
    @cached_property
    def diagnose_fixture_matrix(self) -> dict[str, list[dict[str, Any]]]:
        """The `diagnose-fixture` job's matrix: one entry per resolved pair."""
        return {"include": [cell.model_dump(mode="json") for cell in self.fixture_diagnostics]}

    @computed_field
    @cached_property
    def diagnose_behaviour_matrix(self) -> dict[str, list[dict[str, Any]]]:
        """The `diagnose-behaviour` job's matrix: one entry per resolved pair and supported nightly harness."""
        return {"include": [cell.model_dump(mode="json") for cell in self.behaviour_diagnostics]}

    @computed_field
    @cached_property
    def diagnose_unsupported(self) -> list[dict[str, Any]]:
        """The behaviour cells no job runs, for the report to name."""
        return [cell.model_dump(mode="json") for cell in self.unsupported_diagnostics]

    @computed_field
    @cached_property
    def is_any_pair_diagnosed(self) -> bool:
        """Whether the diagnose jobs have anything to fan out over. GitHub refuses a matrix with no
        entries, so a run whose every pair failed to resolve must skip both jobs rather than hand one
        an empty matrix."""
        return bool(self.fixture_diagnostics) or bool(self.behaviour_diagnostics)


class CiReportContext(FrozenModel):
    """What the Slack report of a scheduled run knows about the run beyond its summaries: where it
    is, what started it, how long it took, and how its jobs ended as GitHub reports them."""

    run_url: str = Field(description="The workflow run's URL")
    trigger: str = Field(description="The event that started the run: schedule, workflow_dispatch, or push")
    duration_seconds: int | None = Field(
        description="Wall clock since the run was queued; None when it could not be read"
    )
    is_live_pass_skipped: bool = Field(description="Whether the run stopped after the oracle passes")
    resolve_result: str = Field(description="The resolve job's result: success, failure, cancelled, or skipped")
    oracle_result: str = Field(description="The oracle job's result, or empty when GitHub reported none")
    evaluate_result: str = Field(description="The evaluate job's result, or empty when GitHub reported none")
    diagnose_fixture_result: str = Field(
        description="The diagnose-fixture job's result, or empty when GitHub reported none"
    )
    diagnose_behaviour_result: str = Field(
        description="The diagnose-behaviour job's result, or empty when GitHub reported none"
    )


class GoalDecision(FrozenModel):
    """What a goal-holding client decided for one exchange: say something else, or stop asking.

    The two questions -- "am I satisfied?" and "what do I say next?" -- are one judgment, so they
    are one model call and one result rather than two.
    """

    is_satisfied: bool = Field(description="Whether the client declared the goal met")
    satisfaction_reason: str = Field(description="Why it is satisfied; empty while it is still asking")
    call: DeciderResult = Field(description="The message to send plus the call's usage accounting")
