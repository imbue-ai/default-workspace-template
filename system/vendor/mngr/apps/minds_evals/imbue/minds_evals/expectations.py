"""Parsing and expansion of a case's `expectations` block.

The eval config authors expectations as a deliverable *kind* with optional refinements; the driver's
evidence collector and the verifier's criteria both need an explicit per-class check list. Expansion
happens exactly once, here, at generation time: the expanded form is written into both copies of the
case config (instruction.md's embedded JSON and tests/case.json), which is what guarantees the
collector can never probe a different set of checks than the judge scores -- and what keeps the
verifier, a stdlib+rewardkit container that cannot import this package, free of expansion logic.
"""

import re
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final
from typing import assert_never

from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.imbue_common.pure import pure
from imbue.minds_evals import ui_flows
from imbue.minds_evals.data_types import AppCheck
from imbue.minds_evals.data_types import CheckClass
from imbue.minds_evals.data_types import DEFAULT_MIN_REGISTERED_APPS
from imbue.minds_evals.data_types import DeliverableExpectation
from imbue.minds_evals.data_types import DeliverableKind
from imbue.minds_evals.data_types import ExpandedExpectations
from imbue.minds_evals.data_types import Expectations
from imbue.minds_evals.data_types import FilesCheck
from imbue.minds_evals.data_types import FilesExpectation
from imbue.minds_evals.data_types import FlowStartPath
from imbue.minds_evals.data_types import FlowSurface
from imbue.minds_evals.data_types import HttpCheck
from imbue.minds_evals.data_types import HttpExpectation
from imbue.minds_evals.data_types import MAX_JUDGE_SCREENSHOTS_CEILING
from imbue.minds_evals.data_types import MINDS_APP_EXPECTED_HTTP_STATUS
from imbue.minds_evals.data_types import ProcessCheck
from imbue.minds_evals.data_types import ProcessCheckKind
from imbue.minds_evals.data_types import ProcessExpectation
from imbue.minds_evals.data_types import REGISTERED_APPS_HTTP_TARGET
from imbue.minds_evals.data_types import RESERVED_MINDS_UI_SURFACE
from imbue.minds_evals.data_types import SKILL_NAME_PATTERN
from imbue.minds_evals.data_types import ScriptedFlowAction
from imbue.minds_evals.data_types import ScriptedFlowActionKind
from imbue.minds_evals.data_types import TimingCheck
from imbue.minds_evals.data_types import TimingExpectation
from imbue.minds_evals.data_types import UiFlow
from imbue.minds_evals.data_types import UiFlowCheck
from imbue.minds_evals.data_types import is_same_skill
from imbue.minds_evals.data_types import scripted_flow_action_problem
from imbue.minds_evals.errors import EvalConfigError

_EXPECTATIONS_KEYS: Final[frozenset[str]] = frozenset(
    {
        "outcome",
        "deliverable",
        "ui_flows",
        "test_commands",
        "fresh_env",
        "max_judge_screenshots",
        "process",
        "timing",
    }
)
_DELIVERABLE_KEYS: Final[frozenset[str]] = frozenset({"kind", "min_registered_apps", "http", "files"})
_HTTP_KEYS: Final[frozenset[str]] = frozenset({"target", "expect_status", "expect_body_regex"})
_FILES_KEYS: Final[frozenset[str]] = frozenset({"glob", "min_count"})
_UI_FLOW_KEYS: Final[frozenset[str]] = frozenset({"name", "actions", "expect", "script", "surface", "start_path"})
# JSON has no comments, so a flow entry may carry a note for its reader under a key with this prefix.
# Only flow entries take one, and it is dropped unread.
_UI_FLOW_COMMENT_KEY_PREFIX: Final[str] = "_comment"
_SCRIPT_ACTION_KEYS: Final[frozenset[str]] = frozenset({"kind", "role", "target", "beside", "text", "amount"})
_PROCESS_KEYS: Final[frozenset[str]] = frozenset({"required_skills", "forbidden_skills", "max_worker_launches"})
_TIMING_KEYS: Final[frozenset[str]] = frozenset({"fast_seconds", "slow_seconds", "requires_no_failures"})

_SKILL_NAME_RE: Final[re.Pattern[str]] = re.compile(SKILL_NAME_PATTERN)

_DEFAULT_FILES_MIN_COUNT: Final[int] = 1

# The id of the one check a timing block expands to, and so of its manifest entry. A constant rather
# than a per-case slug, so a reader and a regrade find the measurement under one name.
TIMING_CHECK_ID: Final[str] = "time_to_goal"


@pure
def slugify(text: str) -> str:
    """An id fragment: lowercase alphanumerics and underscores, collapsed and trimmed. Shared with
    the evidence collector, which builds manifest entry ids by suffixing these check ids."""
    slug = "".join(character if character.isalnum() else "_" for character in text.lower())
    while "__" in slug:
        collapsed = slug.replace("__", "_")
        slug = collapsed
    return slug.strip("_") or "check"


@pure
def _require_mapping(value: object, case_id: str, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvalConfigError("case {!r}: {} must be an object".format(case_id, what))
    return {str(key): entry for key, entry in value.items()}


@pure
def _reject_unknown_keys(raw: Mapping[str, Any], allowed: frozenset[str], case_id: str, what: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise EvalConfigError(
            "case {!r}: unknown key(s) in {}: {} (allowed: {})".format(
                case_id, what, ", ".join(unknown), ", ".join(sorted(allowed))
            )
        )


@pure
def _require_sequence(value: object, case_id: str, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise EvalConfigError("case {!r}: {} must be a list".format(case_id, what))
    return list(value)


@pure
def _parse_http_expectation(raw_entry: object, case_id: str, index: int) -> HttpExpectation:
    what = "deliverable.http[{}]".format(index)
    raw = _require_mapping(raw_entry, case_id, what)
    _reject_unknown_keys(raw, _HTTP_KEYS, case_id, what)
    target = str(raw.get("target") or "").strip()
    if not target:
        raise EvalConfigError("case {!r}: {} needs a 'target'".format(case_id, what))
    raw_status = raw.get("expect_status", MINDS_APP_EXPECTED_HTTP_STATUS)
    if not isinstance(raw_status, int) or isinstance(raw_status, bool):
        raise EvalConfigError("case {!r}: {}.expect_status must be an integer".format(case_id, what))
    return HttpExpectation(
        target=target,
        expect_status=raw_status,
        expect_body_regex=str(raw.get("expect_body_regex") or "").strip(),
    )


@pure
def _parse_files_expectation(raw_entry: object, case_id: str, index: int) -> FilesExpectation:
    what = "deliverable.files[{}]".format(index)
    raw = _require_mapping(raw_entry, case_id, what)
    _reject_unknown_keys(raw, _FILES_KEYS, case_id, what)
    glob = str(raw.get("glob") or "").strip()
    if not glob:
        raise EvalConfigError("case {!r}: {} needs a 'glob'".format(case_id, what))
    raw_min_count = raw.get("min_count", _DEFAULT_FILES_MIN_COUNT)
    if not isinstance(raw_min_count, int) or isinstance(raw_min_count, bool) or raw_min_count < 1:
        raise EvalConfigError("case {!r}: {}.min_count must be a positive integer".format(case_id, what))
    return FilesExpectation(glob=glob, min_count=raw_min_count)


@pure
def _parse_deliverable(raw_entry: object, case_id: str) -> DeliverableExpectation:
    raw = _require_mapping(raw_entry, case_id, "expectations.deliverable")
    _reject_unknown_keys(raw, _DELIVERABLE_KEYS, case_id, "expectations.deliverable")
    raw_kind = str(raw.get("kind") or "").strip()
    if not raw_kind:
        raise EvalConfigError("case {!r}: expectations.deliverable needs a 'kind'".format(case_id))
    # Kinds are written with dashes in the config ("minds-app"); the enum spells them upper-snake.
    try:
        kind = DeliverableKind(raw_kind.replace("-", "_").upper())
    except ValueError:
        raise EvalConfigError(
            "case {!r}: unknown deliverable kind {!r} (known kinds: {})".format(
                case_id,
                raw_kind,
                ", ".join(sorted(member.value.lower().replace("_", "-") for member in DeliverableKind)),
            )
        ) from None
    raw_min_apps = raw.get("min_registered_apps")
    if raw_min_apps is not None and (not isinstance(raw_min_apps, int) or isinstance(raw_min_apps, bool)):
        raise EvalConfigError("case {!r}: deliverable.min_registered_apps must be an integer".format(case_id))
    if raw_min_apps is not None and raw_min_apps < 0:
        raise EvalConfigError("case {!r}: deliverable.min_registered_apps cannot be negative".format(case_id))
    raw_http = _require_sequence(raw.get("http") or [], case_id, "deliverable.http")
    raw_files = _require_sequence(raw.get("files") or [], case_id, "deliverable.files")
    return DeliverableExpectation(
        kind=kind,
        min_registered_apps=raw_min_apps,
        http=tuple(_parse_http_expectation(entry, case_id, index) for index, entry in enumerate(raw_http)),
        files=tuple(_parse_files_expectation(entry, case_id, index) for index, entry in enumerate(raw_files)),
    )


@pure
def _parse_surface(raw: Mapping[str, Any], case_id: str, what: str) -> FlowSurface:
    """Where a flow enters the app. Defaults to the forwarded origin: the app's own label on the
    workspace's agent-keyed origin, where the proxy serves it."""
    raw_surface = str(raw.get("surface") or FlowSurface.ORIGIN.value).strip().lower()
    if raw_surface == RESERVED_MINDS_UI_SURFACE:
        # Reserved, not implemented. Accepting it would drive the app's own origin while the case
        # author believed the Minds chrome was being exercised -- so a works-at-origin-but-broken-
        # when-iframed failure would be reported as a pass, which is the one outcome a reserved
        # field must not produce.
        raise EvalConfigError(
            "case {!r}: {}.surface {!r} is a known but unimplemented surface -- flows would run "
            "against the app's own origin instead, so the Minds chrome would go unexercised. Leave "
            "it unset.".format(case_id, what, RESERVED_MINDS_UI_SURFACE)
        )
    # FlowSurface is a lower-case enum, so its values ARE the config spellings -- unlike
    # DeliverableKind, whose values are upper-snake and need the dash translated.
    try:
        return FlowSurface(raw_surface)
    except ValueError:
        raise EvalConfigError(
            "case {!r}: {} has unknown surface {!r} (known: {}, plus the reserved {!r})".format(
                case_id,
                what,
                raw_surface,
                ", ".join(sorted(member.value for member in FlowSurface)),
                RESERVED_MINDS_UI_SURFACE,
            )
        ) from None


@pure
def _parse_start_path(raw: Mapping[str, Any], case_id: str, what: str) -> FlowStartPath:
    """Where on the app's origin a flow opens. Taken verbatim: whitespace is refused rather than
    trimmed, like every other character that could make the path mean something else."""
    raw_start_path = raw.get("start_path", "")
    if not isinstance(raw_start_path, str):
        raise EvalConfigError("case {!r}: {}.start_path must be a string".format(case_id, what))
    try:
        return FlowStartPath(raw_start_path)
    except InvalidPrimitiveValueError as error:
        raise EvalConfigError("case {!r}: {}.start_path: {}".format(case_id, what, error)) from None


@pure
def _parse_script_action(raw_entry: object, case_id: str, what: str) -> ScriptedFlowAction:
    raw = _require_mapping(raw_entry, case_id, what)
    _reject_unknown_keys(raw, _SCRIPT_ACTION_KEYS, case_id, what)
    raw_kind = raw.get("kind")
    known_kinds = ", ".join(member.value for member in ScriptedFlowActionKind)
    if raw_kind == ui_flows.FlowActionKind.DONE.value:
        raise EvalConfigError(
            "case {!r}: {} is a 'done', which every script ends with on its own (write only the actions "
            "before it: {})".format(case_id, what, known_kinds)
        )
    if not isinstance(raw_kind, str) or raw_kind not in {member.value for member in ScriptedFlowActionKind}:
        raise EvalConfigError(
            "case {!r}: {} has unknown kind {!r} (known: {})".format(case_id, what, raw_kind, known_kinds)
        )
    kind = ScriptedFlowActionKind(raw_kind)
    raw_strings = {field: raw.get(field, "") for field in ("role", "target", "beside", "text")}
    for field, value in raw_strings.items():
        if not isinstance(value, str):
            raise EvalConfigError("case {!r}: {}.{} must be a string".format(case_id, what, field))
    raw_amount = raw.get("amount", 0)
    if not isinstance(raw_amount, int) or isinstance(raw_amount, bool):
        raise EvalConfigError("case {!r}: {}.amount must be an integer".format(case_id, what))
    # Taken verbatim, like a start path: a target is matched against the page's accessible names
    # exactly, and typed text is typed as written.
    problem = scripted_flow_action_problem(
        kind=kind,
        role=raw_strings["role"],
        target=raw_strings["target"],
        beside=raw_strings["beside"],
        text=raw_strings["text"],
        amount=raw_amount,
    )
    if problem:
        raise EvalConfigError("case {!r}: {}: {}".format(case_id, what, problem))
    return ScriptedFlowAction(
        kind=kind,
        role=raw_strings["role"],
        target=raw_strings["target"],
        beside=raw_strings["beside"],
        text=raw_strings["text"],
        amount=raw_amount,
    )


@pure
def parse_flow_script(raw_script: object, case_id: str, what: str) -> tuple[ScriptedFlowAction, ...]:
    """A scripted flow's `script`: a non-empty list of actions in the executor's vocabulary.

    At most `MAX_STEPS_PER_FLOW - 1` of them, because the `done` that closes every script takes a
    step of the flow's budget too; a longer script would be cut off by the budget and recorded as the
    app failing to finish.
    """
    entries = _require_sequence(raw_script, case_id, what)
    if not entries:
        raise EvalConfigError("case {!r}: {} has no actions".format(case_id, what))
    max_action_count = ui_flows.MAX_STEPS_PER_FLOW - 1
    if len(entries) > max_action_count:
        raise EvalConfigError(
            "case {!r}: {} has {} actions, and a script may have at most {} (the closing 'done' takes the "
            "last of a flow's {} steps)".format(
                case_id, what, len(entries), max_action_count, ui_flows.MAX_STEPS_PER_FLOW
            )
        )
    return tuple(
        _parse_script_action(entry, case_id, "{}[{}]".format(what, index)) for index, entry in enumerate(entries)
    )


@pure
def _parse_ui_flow(raw_entry: object, case_id: str, index: int) -> UiFlow:
    what = "expectations.ui_flows[{}]".format(index)
    raw = _require_mapping(raw_entry, case_id, what)
    authored = {key: value for key, value in raw.items() if not key.startswith(_UI_FLOW_COMMENT_KEY_PREFIX)}
    _reject_unknown_keys(authored, _UI_FLOW_KEYS, case_id, what)
    name = str(raw.get("name") or "").strip()
    if not name:
        raise EvalConfigError("case {!r}: {} needs a 'name'".format(case_id, what))
    actions = str(raw.get("actions") or "").strip()
    expect = str(raw.get("expect") or "").strip()
    # A flow is either natural language the verification agent carries out, or a script performed
    # exactly as written -- never both, and never neither. Both kinds state what they expect, since
    # the judge rules on that either way.
    is_scripted = "script" in raw
    if is_scripted and actions:
        raise EvalConfigError("case {!r}: {} carries both 'script' and 'actions'".format(case_id, what))
    if not expect or not (is_scripted or actions):
        raise EvalConfigError(
            "case {!r}: {} needs either 'actions' + 'expect' or 'script' + 'expect'".format(case_id, what)
        )
    return UiFlow(
        name=name,
        actions=actions,
        expect=expect,
        script=parse_flow_script(raw["script"], case_id, "{}.script".format(what)) if is_scripted else (),
        surface=_parse_surface(raw, case_id, what),
        start_path=_parse_start_path(raw, case_id, what),
    )


@pure
def _parse_skill_names(raw_value: object, case_id: str, what: str) -> tuple[str, ...]:
    """The skill names of one process list, checked against the spelling a trajectory can be
    matched on. Two names that slugify alike are rejected, since the slug is the manifest entry's
    id and the second check would overwrite the first."""
    raw_names = _require_sequence(raw_value or [], case_id, what)
    names: list[str] = []
    for index, raw_name in enumerate(raw_names):
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if not _SKILL_NAME_RE.match(name):
            raise EvalConfigError(
                "case {!r}: {}[{}] must be a skill name matching {} (a plugin skill is spelled 'plugin:skill')".format(
                    case_id, what, index, SKILL_NAME_PATTERN
                )
            )
        names.append(name)
    slugs = [slugify(name) for name in names]
    duplicate_slugs = sorted({slug for slug in slugs if slugs.count(slug) > 1})
    if duplicate_slugs:
        raise EvalConfigError(
            "case {!r}: {} has names that collide: {}".format(case_id, what, ", ".join(duplicate_slugs))
        )
    return tuple(names)


@pure
def _parse_process(raw_entry: object, case_id: str) -> ProcessExpectation:
    raw = _require_mapping(raw_entry, case_id, "expectations.process")
    _reject_unknown_keys(raw, _PROCESS_KEYS, case_id, "expectations.process")
    required_skills = _parse_skill_names(raw.get("required_skills"), case_id, "expectations.process.required_skills")
    forbidden_skills = _parse_skill_names(
        raw.get("forbidden_skills"), case_id, "expectations.process.forbidden_skills"
    )
    # Asked with the collector's own matching rule rather than by equality, since that is what
    # decides the checks: a bare forbidden name matches a qualified required one, and the pair could
    # then never both pass.
    contradictory = sorted(
        {
            required if required == forbidden else "{} (as {})".format(required, forbidden)
            for required in required_skills
            for forbidden in forbidden_skills
            if is_same_skill(required, forbidden) or is_same_skill(forbidden, required)
        }
    )
    if contradictory:
        raise EvalConfigError(
            "case {!r}: expectations.process both requires and forbids: {}".format(case_id, ", ".join(contradictory))
        )
    raw_max_launches = raw.get("max_worker_launches")
    if raw_max_launches is not None and (
        not isinstance(raw_max_launches, int) or isinstance(raw_max_launches, bool) or raw_max_launches < 0
    ):
        raise EvalConfigError(
            "case {!r}: expectations.process.max_worker_launches must be a non-negative integer".format(case_id)
        )
    # A block that checks nothing is an authoring mistake rather than a way to opt out: leaving
    # `process` off says the same thing, and accepting the empty block would report a case as
    # grading the agent's process when nothing about it is measured.
    if not required_skills and not forbidden_skills and raw_max_launches is None:
        raise EvalConfigError(
            "case {!r}: expectations.process declares nothing to check -- give it skills or a "
            "'max_worker_launches', or leave the block off".format(case_id)
        )
    return ProcessExpectation(
        required_skills=required_skills,
        forbidden_skills=forbidden_skills,
        max_worker_launches=raw_max_launches,
    )


@pure
def _declared_check_classes(
    deliverable: DeliverableExpectation | None,
    ui_flows: tuple[UiFlow, ...],
    test_commands: tuple[str, ...],
    process: ProcessExpectation | None,
) -> frozenset[CheckClass]:
    """Which expectation classes this case records entries for.

    Read by the timing block, whose prerequisite may only name a class the case declares: a class
    with no entries can never carry a failure, so naming one would read as a prerequisite while
    gating on nothing at all.
    """
    classes: set[CheckClass] = set()
    if deliverable is not None:
        # Whatever refines it, a deliverable always expands into the registry check, the kind's
        # implied root-path probe, and the captured git bundle.
        classes.update({CheckClass.APP, CheckClass.HTTP, CheckClass.BUNDLE})
        if deliverable.files:
            classes.add(CheckClass.FILES)
    if ui_flows:
        classes.add(CheckClass.UI_FLOWS)
    if test_commands:
        classes.add(CheckClass.TEST_COMMAND)
    if process is not None:
        classes.add(CheckClass.PROCESS)
    return frozenset(classes)


@pure
def _parse_timing_anchor(raw: Mapping[str, Any], key: str, case_id: str) -> float:
    """One of the two anchors: required, numeric and strictly positive, since the curve between them
    is taken over logarithms."""
    if key not in raw:
        raise EvalConfigError("case {!r}: expectations.timing needs a {!r}".format(case_id, key))
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvalConfigError("case {!r}: expectations.timing.{} must be a number".format(case_id, key))
    if value <= 0:
        raise EvalConfigError("case {!r}: expectations.timing.{} must be a positive number".format(case_id, key))
    return float(value)


@pure
def _parse_timing_prerequisites(
    raw_value: object, case_id: str, declared_classes: frozenset[CheckClass]
) -> tuple[CheckClass, ...]:
    """The classes whose failure zeroes the measured time. An empty list is a case that measures the
    time on its own terms, which is a legitimate thing to ask for."""
    what = "expectations.timing.requires_no_failures"
    raw_names = _require_sequence(raw_value or [], case_id, what)
    prerequisites: list[CheckClass] = []
    for index, raw_name in enumerate(raw_names):
        where = "{}[{}]".format(what, index)
        name = raw_name.strip().lower() if isinstance(raw_name, str) else ""
        if name == CheckClass.TIMING.value:
            raise EvalConfigError(
                "case {!r}: {} names 'timing' itself, which cannot be its own prerequisite".format(case_id, where)
            )
        if name == CheckClass.TEST_COMMAND.value:
            raise EvalConfigError(
                "case {!r}: {} names 'test_command', which is recorded and never gated, so it cannot "
                "gate the time either".format(case_id, where)
            )
        if name not in {member.value for member in CheckClass}:
            raise EvalConfigError(
                "case {!r}: {} is not an expectation class (known: {})".format(
                    case_id, where, ", ".join(sorted(member.value for member in CheckClass))
                )
            )
        check_class = CheckClass(name)
        if check_class not in declared_classes:
            raise EvalConfigError(
                "case {!r}: {} names {!r}, which this case does not declare, so it could never fail "
                "(declared: {})".format(
                    case_id, where, name, ", ".join(sorted(member.value for member in declared_classes)) or "none"
                )
            )
        prerequisites.append(check_class)
    return tuple(prerequisites)


@pure
def _parse_timing(raw_entry: object, case_id: str, declared_classes: frozenset[CheckClass]) -> TimingExpectation:
    raw = _require_mapping(raw_entry, case_id, "expectations.timing")
    _reject_unknown_keys(raw, _TIMING_KEYS, case_id, "expectations.timing")
    fast_seconds = _parse_timing_anchor(raw, "fast_seconds", case_id)
    slow_seconds = _parse_timing_anchor(raw, "slow_seconds", case_id)
    # Checked here as well as on the model, so a mis-authored config is told which case and which
    # two numbers are the problem rather than reported as a pydantic validation failure.
    if fast_seconds >= slow_seconds:
        raise EvalConfigError(
            "case {!r}: expectations.timing.fast_seconds ({}) must be below slow_seconds ({})".format(
                case_id, fast_seconds, slow_seconds
            )
        )
    return TimingExpectation(
        fast_seconds=fast_seconds,
        slow_seconds=slow_seconds,
        requires_no_failures=_parse_timing_prerequisites(raw.get("requires_no_failures"), case_id, declared_classes),
    )


@pure
def parse_expectations(raw_entry: object, case_id: str) -> Expectations:
    """Validate a case's authored `expectations` block. Unknown keys are rejected rather than
    ignored, so a typo in an eval config fails generation instead of silently scoring nothing."""
    raw = _require_mapping(raw_entry, case_id, "expectations")
    _reject_unknown_keys(raw, _EXPECTATIONS_KEYS, case_id, "expectations")
    outcome = str(raw.get("outcome") or "").strip()
    if not outcome:
        raise EvalConfigError(
            "case {!r}: expectations needs a non-empty 'outcome' (the prose the judge grades against)".format(case_id)
        )
    # A block with no deliverable commissions no app, HTTP or file check, so unless it declares
    # flows or a process block -- which register their own criteria -- its outcome dimension holds
    # nothing but the judge and is graded from the conversation and the always-on capture alone.
    # That composition is deliberately different from a deliverable case's even split between the
    # judge and the programmatic checks, so the two are not comparable score for score; it is the
    # shape a stepped case's early phases need, where the exit criterion is what the client and the
    # agent agreed on rather than what is running.
    raw_deliverable = raw.get("deliverable")
    raw_process = raw.get("process")
    raw_flows = _require_sequence(raw.get("ui_flows") or [], case_id, "expectations.ui_flows")
    raw_commands = _require_sequence(raw.get("test_commands") or [], case_id, "expectations.test_commands")
    test_commands = tuple(str(command).strip() for command in raw_commands)
    if any(not command for command in test_commands):
        raise EvalConfigError("case {!r}: expectations.test_commands has an empty command".format(case_id))
    raw_fresh_env = raw.get("fresh_env", False)
    if not isinstance(raw_fresh_env, bool):
        raise EvalConfigError("case {!r}: expectations.fresh_env must be a boolean".format(case_id))
    if raw_fresh_env:
        # Reserved, not implemented: nothing boots a fresh workspace yet. Silently accepting it would
        # give a case author a completed trial believing the durability of the deliverable was
        # verified, when only the live workspace was ever probed.
        raise EvalConfigError(
            "case {!r}: expectations.fresh_env is a known but unimplemented field -- no fresh "
            "workspace is booted yet, so setting it would verify nothing. Leave it unset.".format(case_id)
        )
    ui_flows = tuple(_parse_ui_flow(entry, case_id, index) for index, entry in enumerate(raw_flows))
    # A flow's name is its evidence directory, so two flows sharing one would overwrite each other's
    # screenshots and step log.
    flow_names = [slugify(flow.name) for flow in ui_flows]
    duplicate_names = sorted({name for name in flow_names if flow_names.count(name) > 1})
    if duplicate_names:
        raise EvalConfigError(
            "case {!r}: expectations.ui_flows has flows whose names collide: {}".format(
                case_id, ", ".join(duplicate_names)
            )
        )
    deliverable = _parse_deliverable(raw_deliverable, case_id) if raw_deliverable is not None else None
    process = _parse_process(raw_process, case_id) if raw_process is not None else None
    # Last, because a timing block's prerequisite is checked against the classes the rest of the
    # block declares.
    raw_timing = raw.get("timing")
    timing = (
        _parse_timing(raw_timing, case_id, _declared_check_classes(deliverable, ui_flows, test_commands, process))
        if raw_timing is not None
        else None
    )
    return Expectations(
        outcome=outcome,
        deliverable=deliverable,
        ui_flows=ui_flows,
        test_commands=test_commands,
        is_fresh_env_enabled=raw_fresh_env,
        max_judge_screenshots=_parse_max_judge_screenshots(raw, case_id),
        process=process,
        timing=timing,
    )


@pure
def _parse_max_judge_screenshots(raw: Mapping[str, Any], case_id: str) -> int | None:
    """How many flow screenshots in all the outcome judge is shown, when the case says. A case whose
    flows would ask for more frames than the verifier's default attaches raises it, so the cap does not
    drop its earliest flows' frames."""
    raw_value = raw.get("max_judge_screenshots")
    if raw_value is None:
        return None
    if (
        not isinstance(raw_value, int)
        or isinstance(raw_value, bool)
        or not 1 <= raw_value <= MAX_JUDGE_SCREENSHOTS_CEILING
    ):
        raise EvalConfigError(
            "case {!r}: expectations.max_judge_screenshots must be an integer from 1 to {}".format(
                case_id, MAX_JUDGE_SCREENSHOTS_CEILING
            )
        )
    return raw_value


@pure
def _implied_http_expectations(kind: DeliverableKind) -> tuple[HttpExpectation, ...]:
    """A Minds app is delivered as a served app tab, so every registered app must answer on its root
    path. The harness probes the app AS DELIVERED and never starts it: an app that was built but
    never started or registered is a delivery failure, not something to repair."""
    match kind:
        case DeliverableKind.MINDS_APP:
            return (
                HttpExpectation(
                    target=REGISTERED_APPS_HTTP_TARGET,
                    expect_status=MINDS_APP_EXPECTED_HTTP_STATUS,
                    expect_body_regex="",
                ),
            )
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _expand_deliverable(
    deliverable: DeliverableExpectation,
) -> tuple[tuple[AppCheck, ...], tuple[HttpCheck, ...], tuple[FilesCheck, ...]]:
    min_registered_apps = (
        deliverable.min_registered_apps if deliverable.min_registered_apps is not None else DEFAULT_MIN_REGISTERED_APPS
    )
    app_checks = (
        AppCheck(
            check_id="app_registered",
            min_registered_apps=min_registered_apps,
            is_supervisord_service_required=True,
        ),
    )
    # The kind's implied probes come first so their ids stay stable as refinements are added.
    http_expectations = (*_implied_http_expectations(deliverable.kind), *deliverable.http)
    http_checks = tuple(
        HttpCheck(
            check_id="http_{}_{}".format(index, slugify(expectation.target)),
            target=expectation.target,
            expect_status=expectation.expect_status,
            expect_body_regex=expectation.expect_body_regex,
        )
        for index, expectation in enumerate(http_expectations)
    )
    files_checks = tuple(
        FilesCheck(check_id="files_{}".format(index), glob=expectation.glob, min_count=expectation.min_count)
        for index, expectation in enumerate(deliverable.files)
    )
    return app_checks, http_checks, files_checks


@pure
def flow_check_actions(actions: str, script: Sequence[ScriptedFlowAction]) -> str:
    """What a flow check declares it does: a scripted flow's script rendered as prose, or a
    model-driven flow's own `actions`. The judge's digest and the flow log read this one field for
    both kinds."""
    return ui_flows.describe_flow_script(script) if script else actions


@pure
def _expand_ui_flows(flows: tuple[UiFlow, ...]) -> tuple[UiFlowCheck, ...]:
    """The flows driven at trial time, each with the id its manifest entry is keyed on."""
    return tuple(
        UiFlowCheck(
            check_id="ui_flow_{}_{}".format(index, slugify(flow.name)),
            name=flow.name,
            actions=flow_check_actions(flow.actions, flow.script),
            script=flow.script,
            expect=flow.expect,
            surface=flow.surface,
            start_path=flow.start_path,
        )
        for index, flow in enumerate(flows)
    )


@pure
def _expand_process(process: ProcessExpectation) -> tuple[ProcessCheck, ...]:
    """One flat check list out of what a process block asks, each with the id its manifest entry is
    keyed on. The launch cap is one check whatever the cap is, so its id needs no discriminator."""
    required_checks = tuple(
        ProcessCheck(
            check_id="skill_required_{}".format(slugify(name)),
            kind=ProcessCheckKind.REQUIRED_SKILL,
            skill=name,
            max_worker_launches=0,
        )
        for name in process.required_skills
    )
    forbidden_checks = tuple(
        ProcessCheck(
            check_id="skill_forbidden_{}".format(slugify(name)),
            kind=ProcessCheckKind.FORBIDDEN_SKILL,
            skill=name,
            max_worker_launches=0,
        )
        for name in process.forbidden_skills
    )
    launch_checks = (
        (
            ProcessCheck(
                check_id="worker_launches",
                kind=ProcessCheckKind.MAX_WORKER_LAUNCHES,
                skill="",
                max_worker_launches=process.max_worker_launches,
            ),
        )
        if process.max_worker_launches is not None
        else ()
    )
    return (*required_checks, *forbidden_checks, *launch_checks)


@pure
def _expand_timing(timing: TimingExpectation) -> tuple[TimingCheck, ...]:
    """The one check a timing block expands to, in the tuple every other class is read out of."""
    return (
        TimingCheck(
            check_id=TIMING_CHECK_ID,
            fast_seconds=timing.fast_seconds,
            slow_seconds=timing.slow_seconds,
            requires_no_failures=timing.requires_no_failures,
        ),
    )


@pure
def expand_expectations(expectations: Expectations) -> ExpandedExpectations:
    """Expand `deliverable.kind` into the explicit per-class check list both consumers act on.

    Expectations that commission no deliverable expand to no app, HTTP or file check and no bundle:
    there is no artifact to probe or to capture. Flows and a process block expand on their own, so a
    case with no deliverable can still carry checks; one that declares none of the three leaves the
    collector its always-on capture and the outcome judge the prose and the conversation.
    """
    app_checks, http_checks, files_checks = (
        _expand_deliverable(expectations.deliverable) if expectations.deliverable is not None else ((), (), ())
    )
    return ExpandedExpectations(
        outcome=expectations.outcome,
        app_checks=app_checks,
        http_checks=http_checks,
        files_checks=files_checks,
        test_commands=expectations.test_commands,
        is_deliverable_bundle_required=expectations.deliverable is not None,
        ui_flow_checks=_expand_ui_flows(expectations.ui_flows),
        is_fresh_env_enabled=expectations.is_fresh_env_enabled,
        max_judge_screenshots=expectations.max_judge_screenshots,
        process_checks=_expand_process(expectations.process) if expectations.process is not None else (),
        timing_checks=_expand_timing(expectations.timing) if expectations.timing is not None else (),
    )
