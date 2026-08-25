import pytest

from imbue.minds_evals.data_types import DeliverableKind
from imbue.minds_evals.data_types import REGISTERED_APPS_HTTP_TARGET
from imbue.minds_evals.errors import EvalConfigError
from imbue.minds_evals.expectations import lower_expectations
from imbue.minds_evals.expectations import parse_expectations


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
                "ui_flows": [{"name": "f", "steps": "s", "expect": "e", "why": "z"}],
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
    with pytest.raises(EvalConfigError, match="either 'steps' \\+ 'expect' or 'script'"):
        parse_expectations(
            {"outcome": "x", "deliverable": {"kind": "minds-app"}, "ui_flows": [{"name": "f", "steps": "open it"}]},
            "todo-app",
        )


def test_parse_expectations_rejects_a_flow_carrying_both_a_script_and_steps() -> None:
    with pytest.raises(EvalConfigError, match="both 'script'"):
        parse_expectations(
            {
                "outcome": "x",
                "deliverable": {"kind": "minds-app"},
                "ui_flows": [{"name": "f", "steps": "s", "expect": "e", "script": "flow.py"}],
            },
            "todo-app",
        )


def test_parse_expectations_rejects_a_scripted_flow_as_unimplemented() -> None:
    # Reserved, not implemented. Accepting it would give a case author a green generation and a
    # completed trial for verification that never ran -- the one failure mode a reserved field
    # must not have. Natural-language flows are unaffected.
    with pytest.raises(EvalConfigError, match="known but unimplemented"):
        parse_expectations(
            {
                "outcome": "x",
                "deliverable": {"kind": "minds-app"},
                "ui_flows": [{"name": "f", "script": "flows/f.py"}],
            },
            "todo-app",
        )


def test_parse_expectations_still_accepts_a_natural_language_flow() -> None:
    expectations = parse_expectations(
        {
            "outcome": "x",
            "deliverable": {"kind": "minds-app"},
            "ui_flows": [{"name": "persistence", "steps": "Open the app. Add a task.", "expect": "still there"}],
        },
        "todo-app",
    )

    assert expectations.ui_flows[0].name == "persistence"
    assert expectations.ui_flows[0].script == ""


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


def test_lower_expectations_expands_the_minds_app_kind_into_explicit_checks() -> None:
    lowered = lower_expectations(parse_expectations({"outcome": "x", "deliverable": {"kind": "minds-app"}}, "todo"))

    assert [check.min_registered_apps for check in lowered.app_checks] == [1]
    assert lowered.app_checks[0].is_supervisord_service_required is True
    assert [(check.target, check.expect_status) for check in lowered.http_checks] == [
        (REGISTERED_APPS_HTTP_TARGET, 200)
    ]
    assert lowered.files_checks == ()
    assert lowered.is_deliverable_bundle_required is True


def test_lower_expectations_merges_refinements_onto_the_implied_checks() -> None:
    lowered = lower_expectations(
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
    assert [check.target for check in lowered.http_checks] == [REGISTERED_APPS_HTTP_TARGET, "todo"]
    assert [check.check_id for check in lowered.http_checks] == ["http_0_registered_apps", "http_1_todo"]
    assert lowered.app_checks[0].min_registered_apps == 3
    assert [(check.check_id, check.min_count) for check in lowered.files_checks] == [("files_0", 1)]


def test_parse_expectations_rejects_a_block_that_would_check_nothing() -> None:
    # Prose-only expectations lower to zero check classes. rewardkit only pools a programmatic
    # reward when criteria exist, so the outcome dimension would silently become 100% judge --
    # double the judge weight every other case carries, which breaks the cross-case comparability
    # the fixed 50/50 split exists to provide.
    with pytest.raises(EvalConfigError, match="needs a 'deliverable'"):
        parse_expectations({"outcome": "Just advice."}, "greeting")
