from typing import Any

import pytest

from imbue.minds_evals.data_types import CheckClass
from imbue.minds_evals.data_types import DeliverableKind
from imbue.minds_evals.data_types import FlowSurface
from imbue.minds_evals.data_types import ProcessCheckKind
from imbue.minds_evals.data_types import REGISTERED_APPS_HTTP_TARGET
from imbue.minds_evals.data_types import ScriptedFlowAction
from imbue.minds_evals.data_types import ScriptedFlowActionKind
from imbue.minds_evals.errors import EvalConfigError
from imbue.minds_evals.expectations import expand_expectations
from imbue.minds_evals.expectations import parse_expectations
from imbue.minds_evals.ui_flows import MAX_STEPS_PER_FLOW


def test_parse_expectations_accepts_an_outcome_and_a_bare_deliverable() -> None:
    expectations = parse_expectations(
        {"outcome": "A working to-do app.", "deliverable": {"kind": "minds-app"}}, "todo-app"
    )

    assert expectations.outcome == "A working to-do app."
    assert expectations.deliverable is not None
    assert expectations.ui_flows == ()
    assert expectations.test_commands == ()
    assert expectations.is_fresh_env_enabled is False


def test_parse_expectations_reads_a_deliverable_and_its_refinements() -> None:
    expectations = parse_expectations(
        {
            "outcome": "Two apps.",
            "deliverable": {
                "kind": "minds-app",
                "min_registered_apps": 2,
                "http": [{"target": "todo", "expect_status": 204, "expect_body_regex": "ok"}],
                "files": [{"glob": "workspace/apps/*/main.py", "min_count": 2}],
            },
            "test_commands": ["uv run pytest -q"],
        },
        "todo-app",
    )

    assert expectations.deliverable is not None
    assert expectations.deliverable.kind == DeliverableKind.MINDS_APP
    assert expectations.deliverable.min_registered_apps == 2
    assert expectations.deliverable.http[0].target == "todo"
    assert expectations.deliverable.http[0].expect_status == 204
    assert expectations.deliverable.http[0].expect_body_regex == "ok"
    assert expectations.deliverable.files[0].min_count == 2
    assert expectations.test_commands == ("uv run pytest -q",)


def test_parse_expectations_requires_outcome_prose() -> None:
    with pytest.raises(EvalConfigError, match="non-empty 'outcome'"):
        parse_expectations({"deliverable": {"kind": "minds-app"}}, "todo-app")


@pytest.mark.parametrize(
    ("raw", "expected_message"),
    [
        ({"outcome": "x", "deliverable": {"kind": "minds-app"}, "outcomes": "typo"}, "unknown key"),
        ({"outcome": "x", "deliverable": {"kind": "minds-app", "min_apps": 1}}, "unknown key"),
        (
            {"outcome": "x", "deliverable": {"kind": "minds-app", "http": [{"target": "a", "status": 200}]}},
            "unknown key",
        ),
        ({"outcome": "x", "deliverable": {"kind": "minds-app", "files": [{"glob": "a", "count": 1}]}}, "unknown key"),
        (
            {
                "outcome": "x",
                "deliverable": {"kind": "minds-app"},
                "ui_flows": [{"name": "f", "actions": "s", "expect": "e", "why": "z"}],
            },
            "unknown key",
        ),
    ],
)
def test_parse_expectations_rejects_unknown_keys(raw: dict[str, object], expected_message: str) -> None:
    # A typo in an eval config must fail generation rather than silently score nothing.
    with pytest.raises(EvalConfigError, match=expected_message):
        parse_expectations(raw, "todo-app")


def test_parse_expectations_rejects_an_unknown_deliverable_kind() -> None:
    with pytest.raises(EvalConfigError, match="unknown deliverable kind 'dataset'"):
        parse_expectations({"outcome": "x", "deliverable": {"kind": "dataset"}}, "todo-app")


def test_parse_expectations_rejects_a_flow_with_neither_steps_nor_script() -> None:
    with pytest.raises(EvalConfigError, match=r"either 'actions' \+ 'expect' or 'script'"):
        parse_expectations(
            {"outcome": "x", "deliverable": {"kind": "minds-app"}, "ui_flows": [{"name": "f", "actions": "open it"}]},
            "todo-app",
        )


def _scripted_flow(script: object, **extra: object) -> dict[str, object]:
    return {
        "outcome": "x",
        "deliverable": {"kind": "minds-app"},
        "ui_flows": [{"name": "f", "expect": "e", "script": script, **extra}],
    }


_ADD_SCRIPT = [
    {"kind": "input", "role": "textbox", "target": "New task", "text": "walk dog"},
    {"kind": "click", "role": "button", "target": "Add"},
]


@pytest.mark.parametrize(
    "flow",
    [
        pytest.param({"name": "f", "actions": "open it"}, id="actions-without-expect"),
        pytest.param({"name": "f", "expect": "e"}, id="expect-alone"),
        pytest.param({"name": "f", "script": _ADD_SCRIPT}, id="script-without-expect"),
    ],
)
def test_parse_expectations_rejects_a_flow_that_is_not_either_kind_whole(flow: dict[str, object]) -> None:
    with pytest.raises(EvalConfigError, match=r"either 'actions' \+ 'expect' or 'script' \+ 'expect'"):
        parse_expectations({"outcome": "x", "deliverable": {"kind": "minds-app"}, "ui_flows": [flow]}, "todo-app")


def test_parse_expectations_rejects_a_flow_carrying_both_a_script_and_actions() -> None:
    with pytest.raises(EvalConfigError, match="both 'script' and 'actions'"):
        parse_expectations(_scripted_flow(_ADD_SCRIPT, actions="Add a task."), "todo-app")


def test_parse_expectations_reads_a_scripted_flows_actions_in_order() -> None:
    expectations = parse_expectations(
        _scripted_flow([*_ADD_SCRIPT, {"kind": "scroll", "amount": -200}, {"kind": "wait"}]), "todo-app"
    )

    assert expectations.ui_flows[0].actions == ""
    assert expectations.ui_flows[0].script == (
        ScriptedFlowAction(kind=ScriptedFlowActionKind.INPUT, role="textbox", target="New task", text="walk dog"),
        ScriptedFlowAction(kind=ScriptedFlowActionKind.CLICK, role="button", target="Add"),
        ScriptedFlowAction(kind=ScriptedFlowActionKind.SCROLL, amount=-200),
        ScriptedFlowAction(kind=ScriptedFlowActionKind.WAIT),
    )


def test_parse_expectations_gives_a_model_driven_flow_no_script() -> None:
    expectations = parse_expectations(
        {
            "outcome": "x",
            "deliverable": {"kind": "minds-app"},
            "ui_flows": [{"name": "persistence", "actions": "Open the app. Add a task.", "expect": "still there"}],
        },
        "todo-app",
    )

    assert expectations.ui_flows[0].name == "persistence"
    assert expectations.ui_flows[0].script == ()


@pytest.mark.parametrize(
    ("entry", "expected_message"),
    [
        pytest.param({"kind": "click", "role": "button"}, "'click' needs target", id="click-without-target"),
        pytest.param({"kind": "click", "target": "Add"}, "'click' needs role", id="click-without-role"),
        pytest.param(
            {"kind": "input", "role": "textbox", "target": "New task"}, "'input' needs text", id="input-without-text"
        ),
        pytest.param({"kind": "keys"}, "'keys' needs text", id="keys-without-text"),
        pytest.param({"kind": "scroll"}, "'scroll' needs amount", id="scroll-without-amount"),
        pytest.param({"kind": "open"}, "'open' needs text", id="open-without-a-path"),
        pytest.param({"kind": "click", "role": " ", "target": "Add"}, "'click' needs role", id="blank-role"),
        pytest.param(
            {"kind": "click", "role": "button", "target": "Add", "text": "walk dog"},
            "'click' does not take text",
            id="click-with-text",
        ),
        pytest.param({"kind": "wait", "amount": 500}, "'wait' does not take amount", id="wait-with-amount"),
        pytest.param({"kind": "reload", "text": "/"}, "'reload' does not take text", id="reload-with-text"),
        pytest.param(
            {"kind": "open", "text": "https://evil.example/"}, "is not a start path", id="open-another-origin"
        ),
        pytest.param({"kind": "open", "text": "//evil.example/"}, "is not a start path", id="open-another-host"),
        pytest.param({"kind": "done"}, "every script ends with on its own", id="done"),
        pytest.param({"kind": "hover", "target": "Add"}, "unknown kind 'hover'", id="unknown-kind"),
        pytest.param({"role": "button", "target": "Add"}, "unknown kind None", id="no-kind"),
        pytest.param({"kind": ["click"]}, "unknown kind", id="kind-not-a-string"),
        pytest.param({"kind": "click", "role": "button", "name": "Add"}, "unknown key", id="unknown-key"),
        pytest.param({"kind": "click", "role": "button", "target": 3}, "target must be a string", id="int-target"),
        pytest.param({"kind": "scroll", "amount": "200"}, "amount must be an integer", id="string-amount"),
        pytest.param({"kind": "scroll", "amount": True}, "amount must be an integer", id="bool-amount"),
        pytest.param(
            {"kind": "click", "role": "checkbox", "target": "walk dog", "beside": "walk dog"},
            "addresses its element by target or by beside, not both",
            id="target-and-beside",
        ),
        pytest.param({"kind": "click", "beside": "walk dog"}, "'click' needs role", id="beside-without-role"),
        pytest.param(
            {"kind": "input", "role": "textbox", "beside": "Notes"},
            "'input' needs text",
            id="located-input-without-text",
        ),
        pytest.param({"kind": "wait", "beside": "walk dog"}, "'wait' does not take beside", id="wait-with-beside"),
        pytest.param({"kind": "click", "role": "checkbox", "beside": 3}, "beside must be a string", id="int-beside"),
        pytest.param("click Add", "must be an object", id="not-an-object"),
    ],
)
def test_parse_expectations_rejects_a_script_action_its_kind_cannot_perform(
    entry: object, expected_message: str
) -> None:
    # Both the action's place and the problem are named, in whichever order the message puts them.
    with pytest.raises(EvalConfigError, match=r"(?=.*ui_flows\[0\]\.script\[1\])(?=.*{})".format(expected_message)):
        parse_expectations(_scripted_flow([_ADD_SCRIPT[0], entry]), "todo-app")


@pytest.mark.parametrize(
    ("script", "expected_message"),
    [
        pytest.param([], "has no actions", id="empty"),
        pytest.param("flows/f.py", "must be a list", id="a-file-name"),
        pytest.param(None, "must be a list", id="null"),
    ],
)
def test_parse_expectations_rejects_a_script_that_is_not_a_list_of_actions(
    script: object, expected_message: str
) -> None:
    with pytest.raises(EvalConfigError, match=expected_message):
        parse_expectations(_scripted_flow(script), "todo-app")


def test_parse_expectations_accepts_a_script_that_leaves_one_step_for_its_closing_done() -> None:
    script = [{"kind": "wait"}] * (MAX_STEPS_PER_FLOW - 1)

    expectations = parse_expectations(_scripted_flow(script), "todo-app")

    assert len(expectations.ui_flows[0].script) == MAX_STEPS_PER_FLOW - 1


def test_parse_expectations_rejects_a_script_that_leaves_no_step_for_its_closing_done() -> None:
    # The flow's step budget would run out on the last action, before the `done`, and the flow
    # would be recorded as the app failing to finish.
    script = [{"kind": "wait"}] * MAX_STEPS_PER_FLOW

    with pytest.raises(EvalConfigError, match="at most {} ".format(MAX_STEPS_PER_FLOW - 1)):
        parse_expectations(_scripted_flow(script), "todo-app")


def test_parse_expectations_rejects_fresh_env_as_unimplemented() -> None:
    # Nothing boots a fresh workspace yet, so setting it would verify nothing while looking verified.
    with pytest.raises(EvalConfigError, match="known but unimplemented"):
        parse_expectations({"outcome": "x", "deliverable": {"kind": "minds-app"}, "fresh_env": True}, "todo-app")


def test_parse_expectations_accepts_fresh_env_left_off() -> None:
    expectations = parse_expectations(
        {"outcome": "x", "deliverable": {"kind": "minds-app"}, "fresh_env": False}, "todo-app"
    )

    assert expectations.is_fresh_env_enabled is False


def test_parse_expectations_rejects_an_empty_test_command() -> None:
    with pytest.raises(EvalConfigError, match="empty command"):
        parse_expectations(
            {"outcome": "x", "deliverable": {"kind": "minds-app"}, "test_commands": ["pytest", "  "]}, "todo-app"
        )


def test_expand_expectations_expands_the_minds_app_kind_into_explicit_checks() -> None:
    expectations = expand_expectations(
        parse_expectations({"outcome": "x", "deliverable": {"kind": "minds-app"}}, "todo")
    )

    assert [check.min_registered_apps for check in expectations.app_checks] == [1]
    assert expectations.app_checks[0].is_supervisord_service_required is True
    assert [(check.target, check.expect_status) for check in expectations.http_checks] == [
        (REGISTERED_APPS_HTTP_TARGET, 200)
    ]
    assert expectations.files_checks == ()
    assert expectations.is_deliverable_bundle_required is True


def test_expand_expectations_merges_refinements_onto_the_implied_checks() -> None:
    expectations = expand_expectations(
        parse_expectations(
            {
                "outcome": "x",
                "deliverable": {
                    "kind": "minds-app",
                    "min_registered_apps": 3,
                    "http": [{"target": "todo", "expect_status": 201}],
                    "files": [{"glob": "workspace/apps/*/main.py"}],
                },
            },
            "todo",
        )
    )

    # The kind's implied probe stays first, so refinements never renumber it.
    assert [check.target for check in expectations.http_checks] == [REGISTERED_APPS_HTTP_TARGET, "todo"]
    assert [check.check_id for check in expectations.http_checks] == ["http_0_registered_apps", "http_1_todo"]
    assert expectations.app_checks[0].min_registered_apps == 3
    assert [(check.check_id, check.min_count) for check in expectations.files_checks] == [("files_0", 1)]


def test_expand_expectations_without_a_deliverable_commissions_nothing_probeable() -> None:
    """Prose-only expectations expand to no checks and no bundle, so the collector records only its
    always-on capture and the outcome dimension is the judge reading the conversation.

    That composition is deliberately different from a deliverable case's even split between the
    judge and the programmatic checks, so the two are not comparable score for score. It is the
    shape a stepped case's early phases need, where the exit criterion is what the client and the
    agent agreed on rather than what is running.
    """
    expanded = expand_expectations(parse_expectations({"outcome": "A mockup was approved."}, "roadmap"))

    assert (expanded.app_checks, expanded.http_checks, expanded.files_checks) == ((), (), ())
    assert not expanded.is_deliverable_bundle_required
    assert expanded.outcome == "A mockup was approved."


def test_expand_expectations_turns_natural_language_flows_into_checks() -> None:
    expectations = expand_expectations(
        parse_expectations(
            {
                "outcome": "x",
                "deliverable": {"kind": "minds-app"},
                "ui_flows": [
                    {"name": "add-complete-delete", "actions": "Add 'buy milk'.", "expect": "'buy milk' is visible."},
                    {"name": "persistence", "actions": "Reload.", "expect": "It survived."},
                ],
            },
            "todo",
        )
    )

    assert [(check.check_id, check.name) for check in expectations.ui_flow_checks] == [
        ("ui_flow_0_add_complete_delete", "add-complete-delete"),
        ("ui_flow_1_persistence", "persistence"),
    ]
    assert expectations.ui_flow_checks[0].expect == "'buy milk' is visible."


def test_expand_expectations_renders_a_scripted_flows_script_as_its_actions() -> None:
    # The judge's digest and the flow log read `actions` for both kinds of flow, so a scripted
    # flow's script reaches them as numbered prose whose numbers are the log's step indices.
    expectations = expand_expectations(
        parse_expectations(
            _scripted_flow([*_ADD_SCRIPT, {"kind": "open", "text": "?latency=300"}, {"kind": "reload"}]), "todo"
        )
    )

    (check,) = expectations.ui_flow_checks
    assert check.actions == (
        "Step 1: type 'walk dog' into the textbox named 'New task'. "
        "Step 2: click the button named 'Add'. "
        "Step 3: open ?latency=300. "
        "Step 4: reload the page."
    )
    assert [entry.kind for entry in check.script] == [
        ScriptedFlowActionKind.INPUT,
        ScriptedFlowActionKind.CLICK,
        ScriptedFlowActionKind.OPEN,
        ScriptedFlowActionKind.RELOAD,
    ]
    assert check.expect == "e"


def test_expand_expectations_keeps_a_model_driven_flows_actions_and_gives_it_no_script() -> None:
    expectations = expand_expectations(
        parse_expectations(
            {
                "outcome": "x",
                "deliverable": {"kind": "minds-app"},
                "ui_flows": [{"name": "f", "actions": "Add a task.", "expect": "e"}],
            },
            "todo",
        )
    )

    assert (expectations.ui_flow_checks[0].actions, expectations.ui_flow_checks[0].script) == ("Add a task.", ())


def test_parse_expectations_rejects_two_flows_whose_names_collide() -> None:
    # A flow's name is its evidence directory, so a collision would have one flow's screenshots and
    # step log overwrite the other's.
    with pytest.raises(EvalConfigError, match="names collide"):
        parse_expectations(
            {
                "outcome": "x",
                "deliverable": {"kind": "minds-app"},
                "ui_flows": [
                    {"name": "add-task", "actions": "s", "expect": "e"},
                    {"name": "add_task", "actions": "s", "expect": "e"},
                ],
            },
            "todo",
        )


def test_parse_expectations_defaults_a_flow_to_the_forwarded_origin() -> None:
    expectations = parse_expectations(
        {
            "outcome": "x",
            "deliverable": {"kind": "minds-app"},
            "ui_flows": [{"name": "persistence", "actions": "s", "expect": "e"}],
        },
        "todo",
    )

    assert expectations.ui_flows[0].surface is FlowSurface.ORIGIN
    assert expand_expectations(expectations).ui_flow_checks[0].surface is FlowSurface.ORIGIN


def test_parse_expectations_rejects_the_minds_ui_surface_as_unimplemented() -> None:
    # Accepting it would drive the app's own origin while the author believed the Minds chrome was
    # exercised -- so a works-at-origin-but-broken-when-iframed failure would report as a pass.
    with pytest.raises(EvalConfigError, match="known but unimplemented surface"):
        parse_expectations(
            {
                "outcome": "x",
                "deliverable": {"kind": "minds-app"},
                "ui_flows": [{"name": "f", "actions": "s", "expect": "e", "surface": "minds-ui"}],
            },
            "todo",
        )


def test_parse_expectations_rejects_an_unknown_surface() -> None:
    # Distinct from the reserved one: a typo should not read as "coming soon".
    with pytest.raises(EvalConfigError, match="unknown surface"):
        parse_expectations(
            {
                "outcome": "x",
                "deliverable": {"kind": "minds-app"},
                "ui_flows": [{"name": "f", "actions": "s", "expect": "e", "surface": "carrier-pigeon"}],
            },
            "todo",
        )


def _flow_with_start_path(start_path: object) -> dict[str, object]:
    return {
        "outcome": "x",
        "deliverable": {"kind": "minds-app"},
        "ui_flows": [{"name": "f", "actions": "s", "expect": "e", "start_path": start_path}],
    }


def test_parse_expectations_opens_a_flow_at_its_apps_root_by_default() -> None:
    expectations = parse_expectations(
        {
            "outcome": "x",
            "deliverable": {"kind": "minds-app"},
            "ui_flows": [{"name": "persistence", "actions": "s", "expect": "e"}],
        },
        "todo",
    )

    assert expand_expectations(expectations).ui_flow_checks[0].start_path == ""


@pytest.mark.parametrize("start_path", ["", "/", "?latency=300", "/?arm_delete=1", "/tasks/3?view=all#notes"])
def test_expand_expectations_carries_a_flows_start_path_into_its_check(start_path: str) -> None:
    expectations = parse_expectations(_flow_with_start_path(start_path), "todo")

    assert expand_expectations(expectations).ui_flow_checks[0].start_path == start_path


@pytest.mark.parametrize(
    "start_path",
    [
        pytest.param("latency=300", id="not-anchored-to-the-origin"),
        pytest.param("#top", id="fragment-only"),
        pytest.param("//evil.example/", id="scheme-relative-host"),
        pytest.param("http://evil.example/", id="absolute-url"),
        pytest.param("https:evil.example", id="scheme-without-slashes"),
        pytest.param("\\\\evil.example", id="backslash-host"),
        pytest.param("/\\evil.example", id="backslash-read-as-a-second-slash"),
        pytest.param("/\t/evil.example", id="tab-stripped-into-a-second-slash"),
        pytest.param("/\n/evil.example", id="newline-stripped-into-a-second-slash"),
        pytest.param(" /tasks", id="leading-space"),
        pytest.param("/tasks ", id="trailing-space"),
        pytest.param("/my tasks", id="inner-space"),
        pytest.param("/café", id="non-ascii"),
    ],
)
def test_parse_expectations_rejects_a_start_path_that_could_leave_the_apps_origin(start_path: str) -> None:
    with pytest.raises(EvalConfigError, match=r"ui_flows\[0\]\.start_path: .* is not a start path"):
        parse_expectations(_flow_with_start_path(start_path), "todo")


@pytest.mark.parametrize("start_path", [300, None, ["/tasks"]])
def test_parse_expectations_rejects_a_start_path_that_is_not_a_string(start_path: object) -> None:
    with pytest.raises(EvalConfigError, match="start_path must be a string"):
        parse_expectations(_flow_with_start_path(start_path), "todo")


def test_parse_expectations_reads_a_scripted_action_that_locates_a_nameless_element() -> None:
    located = {"kind": "click", "role": "checkbox", "beside": "walk dog"}

    expectations = parse_expectations(_scripted_flow([_ADD_SCRIPT[0], located]), "todo-app")

    assert expectations.ui_flows[0].script[1] == ScriptedFlowAction(
        kind=ScriptedFlowActionKind.CLICK, role="checkbox", beside="walk dog"
    )


def test_parse_expectations_ignores_a_comment_key_on_a_flow_entry() -> None:
    commented = _scripted_flow(_ADD_SCRIPT, _comment="why this flow exists", _comment_2="a second note")

    expectations = parse_expectations(commented, "todo-app")

    assert expectations == parse_expectations(_scripted_flow(_ADD_SCRIPT), "todo-app")


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(
            {"outcome": "x", "deliverable": {"kind": "minds-app"}, "_comment": "a note"}, id="on-the-expectations"
        ),
        pytest.param(_scripted_flow([{**_ADD_SCRIPT[0], "_comment": "a note"}]), id="on-a-script-action"),
        pytest.param(_scripted_flow(_ADD_SCRIPT, comment="a note"), id="unprefixed-on-a-flow"),
    ],
)
def test_parse_expectations_takes_a_comment_key_only_on_a_flow_entry(raw: dict[str, object]) -> None:
    with pytest.raises(EvalConfigError, match="unknown key"):
        parse_expectations(raw, "todo-app")


@pytest.mark.parametrize("max_judge_screenshots", [1, 32, 100])
def test_expand_expectations_carries_the_cases_screenshot_ceiling_into_the_expanded_form(
    max_judge_screenshots: int,
) -> None:
    raw = {**_scripted_flow(_ADD_SCRIPT), "max_judge_screenshots": max_judge_screenshots}

    expanded = expand_expectations(parse_expectations(raw, "todo-app"))

    assert expanded.max_judge_screenshots == max_judge_screenshots


def test_expand_expectations_leaves_the_screenshot_ceiling_to_the_verifier_when_the_case_sets_none() -> None:
    assert (
        expand_expectations(parse_expectations(_scripted_flow(_ADD_SCRIPT), "todo-app")).max_judge_screenshots is None
    )


@pytest.mark.parametrize(
    "max_judge_screenshots",
    [
        pytest.param(0, id="zero"),
        pytest.param(-4, id="negative"),
        pytest.param(101, id="over-the-api-image-limit"),
        pytest.param(True, id="bool"),
        pytest.param("32", id="string"),
        pytest.param(32.0, id="float"),
    ],
)
def test_parse_expectations_rejects_a_screenshot_ceiling_outside_what_the_judge_request_takes(
    max_judge_screenshots: object,
) -> None:
    raw = {**_scripted_flow(_ADD_SCRIPT), "max_judge_screenshots": max_judge_screenshots}

    with pytest.raises(EvalConfigError, match="max_judge_screenshots must be an integer from 1 to 100"):
        parse_expectations(raw, "todo-app")


def test_parse_expectations_reads_a_process_block() -> None:
    expectations = parse_expectations(
        {
            "outcome": "A throwaway mock, nothing hardened.",
            "process": {
                "required_skills": ["build-app", "frontend-design"],
                "forbidden_skills": ["crystallize-creation"],
                "max_worker_launches": 0,
            },
        },
        "mock-only",
    )

    assert expectations.process is not None
    assert expectations.process.required_skills == ("build-app", "frontend-design")
    assert expectations.process.forbidden_skills == ("crystallize-creation",)
    assert expectations.process.max_worker_launches == 0


def test_parse_expectations_leaves_a_case_without_a_process_block_unconstrained() -> None:
    expectations = parse_expectations({"outcome": "x", "deliverable": {"kind": "minds-app"}}, "todo-app")

    assert expectations.process is None
    assert expand_expectations(expectations).process_checks == ()


def test_parse_expectations_rejects_an_unknown_process_key() -> None:
    with pytest.raises(EvalConfigError, match="unknown key"):
        parse_expectations({"outcome": "x", "process": {"required_skill": ["build-app"]}}, "mock-only")


def test_parse_expectations_rejects_a_skill_that_is_required_and_forbidden_at_once() -> None:
    # The two lists would expand into checks that can never both pass, so the case could not be
    # satisfied however the agent worked.
    with pytest.raises(EvalConfigError, match="both requires and forbids: build-app"):
        parse_expectations(
            {
                "outcome": "x",
                "process": {"required_skills": ["build-app"], "forbidden_skills": ["build-app"]},
            },
            "mock-only",
        )


def test_parse_expectations_rejects_a_qualified_requirement_its_bare_name_forbids() -> None:
    # The collector matches a bare forbidden name against a plugin-qualified invocation, so this
    # pair could never both pass -- and the oracle, which invokes every required skill, would fail
    # the forbidden check for no reason a reader of the run could see.
    with pytest.raises(EvalConfigError, match=r"both requires and forbids: frontend-design:frontend-design"):
        parse_expectations(
            {
                "outcome": "x",
                "process": {
                    "required_skills": ["frontend-design:frontend-design"],
                    "forbidden_skills": ["frontend-design"],
                },
            },
            "mock-only",
        )


def test_parse_expectations_rejects_an_empty_process_block() -> None:
    # Leaving the block off says the same thing; accepting it would report a case as grading the
    # agent's process while measuring nothing about it.
    with pytest.raises(EvalConfigError, match="declares nothing to check"):
        parse_expectations({"outcome": "x", "process": {}}, "mock-only")


@pytest.mark.parametrize("bad_name", ["", "  ", "-leading-dash", "has space", "slash/name"])
def test_parse_expectations_rejects_a_name_that_is_not_a_skill_name(bad_name: str) -> None:
    with pytest.raises(EvalConfigError, match="must be a skill name"):
        parse_expectations({"outcome": "x", "process": {"required_skills": [bad_name]}}, "mock-only")


def test_parse_expectations_accepts_a_plugin_skill_name() -> None:
    expectations = parse_expectations(
        {"outcome": "x", "process": {"forbidden_skills": ["imbue-code-guardian:autofix"]}}, "mock-only"
    )

    assert expectations.process is not None
    assert expectations.process.forbidden_skills == ("imbue-code-guardian:autofix",)


def test_parse_expectations_rejects_a_negative_worker_launch_cap() -> None:
    with pytest.raises(EvalConfigError, match="non-negative integer"):
        parse_expectations({"outcome": "x", "process": {"max_worker_launches": -1}}, "mock-only")


def test_parse_expectations_rejects_two_skill_names_whose_check_ids_would_collide() -> None:
    # The slug is the manifest entry's id, so the second check would overwrite the first.
    with pytest.raises(EvalConfigError, match="names that collide"):
        parse_expectations({"outcome": "x", "process": {"required_skills": ["build-app", "build_app"]}}, "mock-only")


def test_expand_expectations_turns_a_process_block_into_one_flat_check_list() -> None:
    expanded = expand_expectations(
        parse_expectations(
            {
                "outcome": "x",
                "process": {
                    "required_skills": ["build-app"],
                    "forbidden_skills": ["crystallize-creation"],
                    "max_worker_launches": 0,
                },
            },
            "mock-only",
        )
    )

    assert [(check.check_id, check.kind, check.skill) for check in expanded.process_checks] == [
        ("skill_required_build_app", ProcessCheckKind.REQUIRED_SKILL, "build-app"),
        ("skill_forbidden_crystallize_creation", ProcessCheckKind.FORBIDDEN_SKILL, "crystallize-creation"),
        ("worker_launches", ProcessCheckKind.MAX_WORKER_LAUNCHES, ""),
    ]
    assert expanded.process_checks[-1].max_worker_launches == 0


def test_expand_expectations_leaves_out_a_launch_cap_the_case_did_not_set() -> None:
    expanded = expand_expectations(
        parse_expectations({"outcome": "x", "process": {"required_skills": ["build-app"]}}, "mock-only")
    )

    assert [check.kind for check in expanded.process_checks] == [ProcessCheckKind.REQUIRED_SKILL]


def _timing_case(timing: dict[str, Any]) -> dict[str, Any]:
    """A case that declares a deliverable and a process block, so a timing prerequisite has classes
    it may legitimately name."""
    return {
        "outcome": "A mockup, fast.",
        "deliverable": {"kind": "minds-app"},
        "process": {"required_skills": ["build-app"]},
        "timing": timing,
    }


def test_parse_expectations_reads_a_timing_block() -> None:
    expectations = parse_expectations(
        _timing_case({"fast_seconds": 150, "slow_seconds": 600, "requires_no_failures": ["app"]}), "todo-mock"
    )

    assert expectations.timing is not None
    assert expectations.timing.fast_seconds == 150.0
    assert expectations.timing.slow_seconds == 600.0
    assert expectations.timing.requires_no_failures == (CheckClass.APP,)


def test_parse_expectations_leaves_a_case_without_a_timing_block_unmeasured() -> None:
    expectations = parse_expectations({"outcome": "x", "deliverable": {"kind": "minds-app"}}, "todo-app")

    assert expectations.timing is None
    assert expand_expectations(expectations).timing_checks == ()


def test_parse_expectations_accepts_a_timing_block_with_no_prerequisite() -> None:
    # A case may measure the time on its own terms; that is a different claim from a slow case, and
    # an empty list says it explicitly.
    expectations = parse_expectations(
        _timing_case({"fast_seconds": 10, "slow_seconds": 20, "requires_no_failures": []}), "todo-mock"
    )

    assert expectations.timing is not None
    assert expectations.timing.requires_no_failures == ()


def test_parse_expectations_rejects_a_timing_block_with_an_unknown_key() -> None:
    with pytest.raises(EvalConfigError, match="unknown key"):
        parse_expectations(_timing_case({"fast_seconds": 1, "slow_seconds": 2, "target_seconds": 3}), "todo-mock")


@pytest.mark.parametrize("missing_key", ["fast_seconds", "slow_seconds"])
def test_parse_expectations_rejects_a_timing_block_missing_an_anchor(missing_key: str) -> None:
    # Neither anchor has a defensible default: what counts as fast is the whole per-case judgement.
    anchors: dict[str, Any] = {"fast_seconds": 150, "slow_seconds": 600}
    del anchors[missing_key]

    with pytest.raises(EvalConfigError, match="needs a '{}'".format(missing_key)):
        parse_expectations(_timing_case(anchors), "todo-mock")


@pytest.mark.parametrize("bad_anchor", ["fast", None, True, [150]])
def test_parse_expectations_rejects_an_anchor_that_is_not_a_number(bad_anchor: Any) -> None:
    with pytest.raises(EvalConfigError, match="fast_seconds must be a number"):
        parse_expectations(_timing_case({"fast_seconds": bad_anchor, "slow_seconds": 600}), "todo-mock")


@pytest.mark.parametrize("bad_anchor", [0, -1, -0.5])
def test_parse_expectations_rejects_an_anchor_that_is_not_positive(bad_anchor: float) -> None:
    # The curve is taken over logarithms, so a zero or negative anchor has no curve to sit on.
    with pytest.raises(EvalConfigError, match="fast_seconds must be a positive number"):
        parse_expectations(_timing_case({"fast_seconds": bad_anchor, "slow_seconds": 600}), "todo-mock")


def test_parse_expectations_rejects_anchors_that_are_not_in_order() -> None:
    with pytest.raises(EvalConfigError, match=r"fast_seconds \(600.0\) must be below slow_seconds \(600.0\)"):
        parse_expectations(_timing_case({"fast_seconds": 600, "slow_seconds": 600}), "todo-mock")


def test_parse_expectations_rejects_a_prerequisite_the_case_does_not_declare() -> None:
    # A class with no entries can never carry a failure, so the block would read as gated while
    # gating on nothing.
    with pytest.raises(EvalConfigError, match="which this case does not declare"):
        parse_expectations(
            _timing_case({"fast_seconds": 1, "slow_seconds": 2, "requires_no_failures": ["ui_flows"]}), "todo-mock"
        )


def test_parse_expectations_rejects_a_prerequisite_that_is_not_an_expectation_class() -> None:
    with pytest.raises(EvalConfigError, match="is not an expectation class"):
        parse_expectations(
            _timing_case({"fast_seconds": 1, "slow_seconds": 2, "requires_no_failures": ["apps"]}), "todo-mock"
        )


def test_parse_expectations_rejects_timing_as_its_own_prerequisite() -> None:
    with pytest.raises(EvalConfigError, match="cannot be its own prerequisite"):
        parse_expectations(
            _timing_case({"fast_seconds": 1, "slow_seconds": 2, "requires_no_failures": ["timing"]}), "todo-mock"
        )


def test_parse_expectations_rejects_test_commands_as_a_timing_prerequisite() -> None:
    # Test commands are recorded and never gated; letting one gate the clock would gate them after
    # all, on a case whose prompts may never have mentioned tests.
    with pytest.raises(EvalConfigError, match="recorded and never gated"):
        parse_expectations(
            {
                "outcome": "x",
                "deliverable": {"kind": "minds-app"},
                "test_commands": ["pytest"],
                "timing": {"fast_seconds": 1, "slow_seconds": 2, "requires_no_failures": ["test_command"]},
            },
            "todo-mock",
        )


def test_expand_expectations_turns_a_timing_block_into_one_check() -> None:
    expanded = expand_expectations(
        parse_expectations(
            _timing_case({"fast_seconds": 150, "slow_seconds": 600, "requires_no_failures": ["app"]}), "todo-mock"
        )
    )

    assert [
        (check.check_id, check.fast_seconds, check.slow_seconds, check.requires_no_failures)
        for check in expanded.timing_checks
    ] == [("time_to_goal", 150.0, 600.0, (CheckClass.APP,))]
