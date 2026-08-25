from enum import auto
from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel

# A prompts entry equal to this sentinel is role-played by the decider model
# instead of being sent verbatim. It cannot be the first prompt (there is no
# transcript to decide from yet).
DECIDE_SENTINEL: Final[str] = "DECIDE_FROM_PERSONA"

# The default-workspace-template (dwt) each eval case is cloned from.
DEFAULT_DWT_REPO: Final[str] = "https://github.com/imbue-ai/default-workspace-template.git"
DEFAULT_DWT_BRANCH: Final[str] = "main"

DEFAULT_TIMEOUT_SECONDS: Final[float] = 3600.0

# Seed value for the wordiness guard until PR2 measures real old-harness batch
# averages; overridable per eval config via "avg_word_count_baseline".
DEFAULT_AVG_WORD_COUNT_BASELINE: Final[float] = 120.0

# Wall-clock the driver's evidence-collection phase gets after the conversation
# ends; overridable per eval config via "verification_timeout_seconds".
#
# Sized for a case that declares UI flows, which dominate the phase: each flow is bounded
# separately and the rest of the capture takes ~2 minutes. It is a deadline, not a reservation --
# a case with no flows finishes in a couple of minutes and the rest is never spent -- so the
# generous default costs nothing beyond a longer harbor agent timeout.
DEFAULT_VERIFICATION_TIMEOUT_SECONDS: Final[float] = 1800.0

# Apps every Minds workspace serves out of the box -- the workspace template's own, which live
# under its `system/apps/<name>` tree and register through the same path a delivered app does. A
# deliverable app is any entry in the workspace's registry that is not one of these.
#
# The list has to track what the template ships: a builtin missing from it is counted as something
# the agent delivered, which both inflates the delivered count and charges the agent for a builtin
# that happens to be unhealthy.
BUILTIN_APP_NAMES: Final[tuple[str, ...]] = ("system_interface", "terminal", "browser", "files")

# What "kind": "minds-app" implies when nothing refines it: one delivered app,
# answering 200 on its root path.
DEFAULT_MIN_REGISTERED_APPS: Final[int] = 1
MINDS_APP_EXPECTED_HTTP_STATUS: Final[int] = 200

# The http-check target that fans out to every registered non-builtin app rather
# than naming one service.
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

    ORIGIN goes straight to the app's forwarded origin -- the URL the client's app tab iframes --
    which is one origin with no frame-piercing and exercises the real serving path (forward proxy,
    tunnel, label origin, origin-scoped cookies).

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
    """A lowered registry/service check: enough delivered apps are registered and their services run."""

    check_id: str = Field(description="Stable id, used as the manifest entry's id prefix")
    min_registered_apps: int = Field(description="How many non-builtin apps must appear in the registry")
    is_supervisord_service_required: bool = Field(description="Whether each registered app's service must be running")


class HttpCheck(FrozenModel):
    """A lowered HTTP probe. A 'registered-apps' target fans out to one probe per delivered app."""

    check_id: str = Field(description="Stable id, used as the manifest entry's id prefix")
    target: str = Field(description="'registered-apps' or a service name")
    expect_status: int = Field(description="The HTTP status the probe must observe")
    expect_body_regex: str = Field(description="Regex the captured body head must match (empty means unchecked)")


class FilesCheck(FrozenModel):
    """A lowered file-inventory check, evaluated at grade time against the captured inventory."""

    check_id: str = Field(description="Stable id, used as the manifest entry's id")
    glob: str = Field(description="Glob matched against paths relative to the workspace home tree")
    min_count: int = Field(description="How many inventory entries the glob must match")


class UiFlowCheck(FrozenModel):
    """A lowered UI flow: one natural-language flow the verification agent drives at trial time.

    Only flows authored as `steps` + `expect` lower to a check; the reserved `script` and
    `minds-ui` spellings are rejected at parse time and never reach here.
    """

    check_id: str = Field(description="Stable id, used as the manifest entry's id")
    name: str = Field(description="The flow's name; names its evidence directory under flows/")
    steps: str = Field(description="Natural-language step sequence the verification agent executes")
    expect: str = Field(description="The verifiable end condition the agent judges the final state against")
    surface: FlowSurface = Field(description="Where the flow enters the app; the forwarded origin in v1")


class LoweredExpectations(FrozenModel):
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
    """Which lowered expectation class a manifest entry belongs to; the verifier registers one
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
    check_class: CheckClass = Field(description="The lowered expectation class this entry feeds")
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


class RegisteredApp(FrozenModel):
    """One entry of the workspace's app registry (data/.state/apps.toml)."""

    name: str = Field(description="The registered service name")
    url: str = Field(description="The workspace-local origin the app is served on")
    # The unguessable `<name>-<rand>` origin label forward_port.py mints, and the component the
    # forwarded origin is built from: `https://<label>.host-<hex>.localhost:<port>/`. The forward
    # proxy maps the label back to the service name itself, so the label -- not the name -- is what
    # a URL must carry. Defaulted rather than required because "no label" is a real registry state
    # and not an omission: a row written before labels existed has none, and forward routes it under
    # its own name.
    label: str = Field(default="", description="The service's origin label; empty when the row has none")
    is_builtin: bool = Field(description="Whether this is one of the apps every workspace ships with")
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
    is_expectations_declared: bool = Field(description="Whether the case declared expectations at all")
    is_evidence_complete: bool = Field(description="True when no entry has status ERROR")
    started_at: str = Field(description="UTC ISO timestamp the collection phase began")
    phases: tuple[PhaseTiming, ...] = Field(description="Wall-clock per collection phase")
    entries: tuple[ManifestEntry, ...] = Field(description="Every recorded probe, in collection order")


class PersonaCase(FrozenModel):
    """One persona case from an eval config: an id, an optional persona, and one prompt per turn."""

    case_id: str = Field(description="Stable case id; names the task directory and the trial")
    persona: str = Field(description="Client persona role-played on DECIDE_FROM_PERSONA turns (may be empty)")
    prompts: tuple[str, ...] = Field(description="One entry per turn: a literal message or DECIDE_FROM_PERSONA")
    expectations: Expectations | None = Field(description="What the delivered artifact must be, if the case says")


class EvalConfig(FrozenModel):
    """A validated eval config file: the mngr branch under test plus the persona cases."""

    mngr_branch: str = Field(description="The mngr branch the box is built from")
    dwt_repo: str = Field(description="Workspace template repo each case is cloned from")
    dwt_branch: str = Field(description="Workspace template branch")
    timeout_seconds: float = Field(description="Per-case wall-clock budget in seconds")
    verification_timeout_seconds: float = Field(description="Wall-clock budget for the evidence-collection phase")
    avg_word_count_baseline: float = Field(description="Baseline for the verifier's wordiness guard")
    cases: tuple[PersonaCase, ...] = Field(description="The persona cases, one task each")


class CaseConfig(FrozenModel):
    """The full per-case config carried in the task instruction and in tests/case.json."""

    case_id: str = Field(description="Stable case id")
    persona: str = Field(description="Client persona for DECIDE_FROM_PERSONA turns (may be empty)")
    prompts: tuple[str, ...] = Field(description="One entry per turn")
    timeout_seconds: float = Field(description="Per-case wall-clock budget in seconds")
    verification_timeout_seconds: float = Field(description="Wall-clock budget for the evidence-collection phase")
    mngr_branch: str = Field(description="The mngr branch the box was built from")
    mngr_sha: str = Field(description="Exact mngr SHA resolved at generation time")
    dwt_repo: str = Field(description="Workspace template repo")
    dwt_branch: str = Field(description="Workspace template branch the SHA was resolved from")
    dwt_sha: str = Field(description="Exact workspace template SHA resolved at generation time")
    avg_word_count_baseline: float = Field(description="Baseline for the verifier's wordiness guard")
    # The lowered form is what both the collector and the verifier act on; the authored form rides
    # along so a reader of instruction.md or case.json can see what the config actually said.
    expectations: LoweredExpectations | None = Field(description="The lowered expectations, if the case has any")
    authored_expectations: Expectations | None = Field(description="The expectations exactly as authored")


class Transcript(FrozenModel):
    """The conversation so far, as raw system_interface events (verbatim schema)."""

    events: tuple[dict[str, Any], ...] = Field(description="Raw events from the workspace system_interface")


class DeciderResult(FrozenModel):
    """One decider (simulated-user) model call: the message plus usage accounting."""

    message: str = Field(description="The client's next message")
    model: str = Field(description="The decider model used (empty when the fallback was used)")
    input_token_count: int = Field(description="Input tokens consumed by the call (0 on fallback)")
    output_token_count: int = Field(description="Output tokens consumed by the call (0 on fallback)")
    is_fallback: bool = Field(description="Whether the literal fallback message was used")
