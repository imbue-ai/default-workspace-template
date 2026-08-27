"""Parsing and expansion of a case's `expectations` block.

The eval config authors expectations as a deliverable *kind* with optional refinements; the driver's
evidence collector and the verifier's criteria both need an explicit per-class check list. Expansion
happens exactly once, here, at generation time: the expanded form is written into both copies of the
case config (instruction.md's embedded JSON and tests/case.json), which is what guarantees the
collector can never probe a different set of checks than the judge scores -- and what keeps the
verifier, a stdlib+rewardkit container that cannot import this package, free of expansion logic.
"""

from collections.abc import Mapping
from typing import Any
from typing import Final
from typing import assert_never

from imbue.imbue_common.pure import pure
from imbue.minds_evals.data_types import AppCheck
from imbue.minds_evals.data_types import DEFAULT_MIN_REGISTERED_APPS
from imbue.minds_evals.data_types import DeliverableExpectation
from imbue.minds_evals.data_types import DeliverableKind
from imbue.minds_evals.data_types import ExpandedExpectations
from imbue.minds_evals.data_types import Expectations
from imbue.minds_evals.data_types import FilesCheck
from imbue.minds_evals.data_types import FilesExpectation
from imbue.minds_evals.data_types import FlowSurface
from imbue.minds_evals.data_types import HttpCheck
from imbue.minds_evals.data_types import HttpExpectation
from imbue.minds_evals.data_types import MINDS_APP_EXPECTED_HTTP_STATUS
from imbue.minds_evals.data_types import REGISTERED_APPS_HTTP_TARGET
from imbue.minds_evals.data_types import RESERVED_MINDS_UI_SURFACE
from imbue.minds_evals.data_types import UiFlow
from imbue.minds_evals.data_types import UiFlowCheck
from imbue.minds_evals.errors import EvalConfigError

_EXPECTATIONS_KEYS: Final[frozenset[str]] = frozenset(
    {"outcome", "deliverable", "ui_flows", "test_commands", "fresh_env"}
)
_DELIVERABLE_KEYS: Final[frozenset[str]] = frozenset({"kind", "min_registered_apps", "http", "files"})
_HTTP_KEYS: Final[frozenset[str]] = frozenset({"target", "expect_status", "expect_body_regex"})
_FILES_KEYS: Final[frozenset[str]] = frozenset({"glob", "min_count"})
_UI_FLOW_KEYS: Final[frozenset[str]] = frozenset({"name", "steps", "expect", "script", "surface"})

_DEFAULT_FILES_MIN_COUNT: Final[int] = 1


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
def _parse_ui_flow(raw_entry: object, case_id: str, index: int) -> UiFlow:
    what = "expectations.ui_flows[{}]".format(index)
    raw = _require_mapping(raw_entry, case_id, what)
    _reject_unknown_keys(raw, _UI_FLOW_KEYS, case_id, what)
    name = str(raw.get("name") or "").strip()
    if not name:
        raise EvalConfigError("case {!r}: {} needs a 'name'".format(case_id, what))
    steps = str(raw.get("steps") or "").strip()
    expect = str(raw.get("expect") or "").strip()
    script = str(raw.get("script") or "").strip()
    # A flow is either natural language the verification agent executes, or a per-case script for a
    # UI with stable selectors -- never both, and never neither.
    if script and (steps or expect):
        raise EvalConfigError("case {!r}: {} carries both 'script' and 'steps'/'expect'".format(case_id, what))
    if not script and not (steps and expect):
        raise EvalConfigError("case {!r}: {} needs either 'steps' + 'expect' or 'script'".format(case_id, what))
    if script:
        # The field is reserved, not implemented. Accepting it would hand a case author a green
        # generation and a completed trial for verification that never ran -- the one failure mode a
        # reserved field must not have.
        raise EvalConfigError(
            "case {!r}: {} uses 'script', which is a known but unimplemented field -- scripted flow "
            "execution has no semantics yet, so nothing would run it. Use 'steps' + 'expect'.".format(case_id, what)
        )
    return UiFlow(name=name, steps=steps, expect=expect, script=script, surface=_parse_surface(raw, case_id, what))


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
    raw_deliverable = raw.get("deliverable")
    if raw_deliverable is None:
        # An expectations block with no deliverable expands to zero check classes, so its outcome
        # dimension would hold nothing but the judge: rewardkit pools programmatic criteria into a
        # reward only when some exist, so the judge would silently carry the entire dimension rather
        # than the half it carries everywhere else, and this case's reward would not be comparable
        # with any other's. Rejected until a degenerate composition is deliberately specified.
        raise EvalConfigError(
            "case {!r}: expectations needs a 'deliverable' -- with none, nothing is checked "
            "programmatically and the outcome score would be the judge alone, which is not "
            "comparable with cases that carry checks".format(case_id)
        )
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
    return Expectations(
        outcome=outcome,
        deliverable=_parse_deliverable(raw_deliverable, case_id),
        ui_flows=ui_flows,
        test_commands=test_commands,
        is_fresh_env_enabled=raw_fresh_env,
    )


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
def _expand_ui_flows(flows: tuple[UiFlow, ...]) -> tuple[UiFlowCheck, ...]:
    """The flows the verification agent drives, each with the id its manifest entry is keyed on.

    Every flow reaching here is natural language: `parse_expectations` rejects the reserved `script`
    field outright, because scripted execution has no semantics yet. The assertion holds that line
    from this side -- if scripts are ever accepted at parse time again, expanding one into an
    ordinary check would silently commission verification that nothing runs.
    """
    assert not any(flow.script for flow in flows), "scripted flows are rejected at parse time"
    return tuple(
        UiFlowCheck(
            check_id="ui_flow_{}_{}".format(index, slugify(flow.name)),
            name=flow.name,
            steps=flow.steps,
            expect=flow.expect,
            surface=flow.surface,
        )
        for index, flow in enumerate(flows)
    )


@pure
def expand_expectations(expectations: Expectations) -> ExpandedExpectations:
    """Expand `deliverable.kind` into the explicit per-class check list both consumers act on."""
    # Guaranteed by parse_expectations, which rejects a block that would expand to no checks at all.
    assert expectations.deliverable is not None, "expectations must commission a deliverable"
    app_checks, http_checks, files_checks = _expand_deliverable(expectations.deliverable)
    return ExpandedExpectations(
        outcome=expectations.outcome,
        app_checks=app_checks,
        http_checks=http_checks,
        files_checks=files_checks,
        test_commands=expectations.test_commands,
        is_deliverable_bundle_required=True,
        ui_flow_checks=_expand_ui_flows(expectations.ui_flows),
        is_fresh_env_enabled=expectations.is_fresh_env_enabled,
    )
