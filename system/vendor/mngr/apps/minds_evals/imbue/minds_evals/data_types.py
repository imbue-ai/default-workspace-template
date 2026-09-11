from enum import auto
from functools import cached_property
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Final
from typing import Self

from pydantic import Field
from pydantic import StringConstraints
from pydantic import computed_field
from pydantic import model_validator

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals.errors import CapturedFileError

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

# The http-check target that fans out to every delivered app rather than naming
# one service.
REGISTERED_APPS_HTTP_TARGET: Final[str] = "registered-apps"


class DeliverableKind(UpperCaseStrEnum):
    """The shape of artifact a case commissions; each kind implies a standard set of checks."""

    MINDS_APP = auto()


class HttpExpectation(FrozenModel):
    """One authored HTTP probe: what to hit and what the response must look like."""

    target: str = Field(description="'registered-apps' (fan out to every delivered app) or a service name")
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


class UiFlow(FrozenModel):
    """One behavioral flow through the delivered UI, exactly as authored."""

    name: str = Field(description="Stable flow name; names the flow's evidence directory")
    steps: str = Field(description="Natural-language step sequence (empty when the flow carries a script)")
    expect: str = Field(description="The verifiable end condition (empty when the flow carries a script)")
    script: str = Field(description="Per-case script file for flows anchored in a known app (empty otherwise)")
    surface: FlowSurface = Field(description="Where the flow enters the app; defaults to the forwarded origin")


class Expectations(FrozenModel):
    """A case's authored outcome expectations, exactly as written in the eval config."""

    outcome: str = Field(description="The prose the outcome judge grades the delivered artifact against")
    deliverable: DeliverableExpectation | None = Field(description="What the case commissions, if anything")
    ui_flows: tuple[UiFlow, ...] = Field(description="Behavioral flows the verification agent drives through the UI")
    test_commands: tuple[str, ...] = Field(description="Commands run in the delivered repo; recorded, never gated")
    is_fresh_env_enabled: bool = Field(description="Reserved: also boot the deliverable in a fresh workspace")


class AppCheck(FrozenModel):
    """An expanded registry/service check: enough delivered apps are registered and their services run."""

    check_id: str = Field(description="Stable id, used as the manifest entry's id prefix")
    min_registered_apps: int = Field(description="How many delivered apps must appear in the registry")
    is_supervisord_service_required: bool = Field(description="Whether each registered app's service must be running")


class HttpCheck(FrozenModel):
    """An expanded HTTP probe. A 'registered-apps' target fans out to one probe per delivered app."""

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
    """An expanded UI flow: one natural-language flow the verification agent drives at trial time.

    Only flows authored as `steps` + `expect` expand to a check; the reserved `script` and
    `minds-ui` spellings are rejected at parse time and never reach here.
    """

    check_id: str = Field(description="Stable id, used as the manifest entry's id")
    name: str = Field(description="The flow's name; names its evidence directory under flows/")
    steps: str = Field(description="Natural-language step sequence the verification agent executes")
    expect: str = Field(description="The verifiable end condition the agent judges the final state against")
    surface: FlowSurface = Field(description="Where the flow enters the app; the forwarded origin in v1")


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


class CheckStatus(LowerCaseStrEnum):
    """How one recorded probe came out. The distinction the whole grading policy rests on is
    FAILED (the workspace fell short) versus ERROR (the harness could not find out)."""

    PASSED = auto()
    FAILED = auto()
    ERROR = auto()


class CheckClass(LowerCaseStrEnum):
    """Which expanded expectation class a manifest entry belongs to; the verifier registers one
    programmatic criterion per scored class and ignores the rest."""

    APP = auto()
    HTTP = auto()
    FILES = auto()
    BUNDLE = auto()
    TEST_COMMAND = auto()
    UI_FLOWS = auto()


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
    the worker is listed, DESTROYED when only mngr's preserved copy of it remains, and UNKNOWN when
    neither the listing nor a preserved directory says."""

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


class WorkerListingEntry(FrozenModel):
    """One agent as `mngr list --format json` reported it inside the workspace at collection time."""

    agent_id: str = Field(description="The mngr agent id")
    name: str = Field(description="The agent name")
    agent_type: str = Field(description="The agent type (claude, codex, ...)")
    state: WorkerState = Field(description="The lifecycle state, folded into what the capture cares about")
    work_dir: str = Field(description="The agent's work dir, which launch commands' paths are relative to")


class WorkerCapture(FrozenModel):
    """What the evidence phase brought out for one launched worker: its ATIF document, its stream, and the
    report it pushed back to its lead, each recorded on its own."""

    launch: WorkerLaunch = Field(description="The launch this capture answers")
    agent_id: str = Field(description="The worker's mngr agent id; empty when it could not be resolved")
    agent_type: str = Field(description="The worker's agent type from the listing; empty when it was not listed")
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
    # dwt tip, checks it reproduces base_sha, and unbundles the agent's commits onto it -- which only
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
    """A provider lane a workspace can be signed in on without a human at a device prompt.

    The lane decides the harness, because that is how the product itself decides it: a chat runs on
    the harness of the lane its account was minted on. ANTHROPIC serves claude; the rest serve
    pi-coding, differing in whose key they take. The `openai` lane (codex) has no member here because
    its sign-in cannot be driven by a run.
    """

    ANTHROPIC = auto()
    API_KEY = auto()
    OPENROUTER = auto()
    OPENCODE_GO = auto()


@pure
def lane_id(lane: HarnessLane) -> str:
    """The lane as the workspace's accounts API and the command line spell it: dashes, not
    underscores (`--ak lane=api-key`)."""
    return lane.value.replace("_", "-")


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
    # (`POST /api/agents/<id>/model`), since one such call is what this field reports on.
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
    expectations: Expectations | None = Field(
        description="What this step is graded against; None grades it on gates and quality alone"
    )
    min_reward: StepMinReward | None = Field(
        description="The reward floor below which harbor aborts the remaining steps; None never aborts"
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


class PairDecision(LowerCaseStrEnum):
    """What a scheduled run decided about one (mngr, dwt) pair.

    UNRESOLVED means one of its refs is not on its remote, so it has no SHAs and no cells; SKIP means
    every one of its cells is already green; RUN means at least one cell runs, and with it the pair's
    oracle pass.
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
    """One arm of a scheduled run: a frozen pair times one named harness config, with everything
    the cell's job needs spelled out so the workflow reads values and composes nothing.

    Every field is a string or an enum on purpose: the cell is a GitHub Actions matrix entry, and
    `harbor_args` is therefore a JSON-encoded list rather than a list.
    """

    pair: str = Field(description="The pair's name")
    harness_config: str = Field(description="The harness config's name")
    mngr_ref: str = Field(description="The mngr ref the pair was asked for")
    mngr_sha: str = Field(description="The mngr SHA the pair froze to")
    dwt_ref: str = Field(description="The workspace-template ref the pair was asked for")
    dwt_sha: str = Field(description="The workspace-template SHA the pair froze to")
    config: str = Field(description="The repo-relative eval config the dataset is generated from")
    lane_key_env: str = Field(description="The environment variable the driver reads the lane's key from")
    harbor_args: str = Field(description="JSON-encoded list of the `--ak` arguments appended to the run line")
    cache_key: str = Field(description="The green marker's cache key: pair, both SHAs, config, and the harness config")
    decision: CellDecision = Field(description="Whether the cell runs or its green marker already holds")


class CiMatrix(FrozenModel):
    """What the scheduled run's resolve job decided, for the jobs after it: the pairs and cells it
    considered, and the two matrices (oracle passes per running pair, live passes per running cell)
    read straight out of the serialized form."""

    config: str = Field(description="The repo-relative eval config every cell generates its dataset from")
    pairs: tuple[DecidedPair, ...] = Field(description="Every pair considered, skipped and unresolved ones included")
    cells: tuple[MatrixCell, ...] = Field(description="Every cell of every resolved pair, skipped ones included")

    @computed_field
    @cached_property
    def oracle_matrix(self) -> dict[str, list[dict[str, Any]]]:
        """The `oracle` job's matrix: one entry per pair that has a cell to run."""
        return {
            "include": [
                pair.model_dump(mode="json", exclude={"decision"})
                for pair in self.pairs
                if pair.decision is PairDecision.RUN
            ]
        }

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


class GoalDecision(FrozenModel):
    """What a goal-holding client decided for one exchange: say something else, or stop asking.

    The two questions -- "am I satisfied?" and "what do I say next?" -- are one judgment, so they
    are one model call and one result rather than two.
    """

    is_satisfied: bool = Field(description="Whether the client declared the goal met")
    satisfaction_reason: str = Field(description="Why it is satisfied; empty while it is still asking")
    call: DeciderResult = Field(description="The message to send plus the call's usage accounting")
