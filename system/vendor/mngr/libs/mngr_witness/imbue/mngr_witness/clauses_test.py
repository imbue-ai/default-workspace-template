from pathlib import Path

from imbue.mngr_behaviors.data_types import BehaviorStep
from imbue.mngr_behaviors.data_types import BehaviorUnit
from imbue.mngr_behaviors.data_types import BehaviorUnitKind
from imbue.mngr_witness.clauses import unit_clauses


def _unit(kind: BehaviorUnitKind, name: str, steps: tuple[tuple[str, str], ...]) -> BehaviorUnit:
    return BehaviorUnit(
        coordinate="area.unit",
        kind=kind,
        name=name,
        file=Path("area/feature.feature"),
        line=3,
        tags=("unit",),
        steps=tuple(BehaviorStep(keyword=keyword, text=text) for keyword, text in steps),
        parent=None,
    )


def test_a_rules_only_clause_is_the_property_its_name_states() -> None:
    rule = _unit(BehaviorUnitKind.RULE, "A one-time code grants at most one session, ever", ())

    assert unit_clauses(rule) == ("A one-time code grants at most one session, ever",)


def test_a_scenarios_clauses_are_its_then_steps_and_their_continuations() -> None:
    scenario = _unit(
        BehaviorUnitKind.SCENARIO,
        "Reaching the endpoint establishes the session",
        (
            ("Given", "a running proxy"),
            ("When", "a browser reaches the endpoint carrying the code"),
            ("Then", "the code is spent"),
            ("And", "a session cookie is set"),
            ("But", "no second cookie is set"),
        ),
    )

    assert unit_clauses(scenario) == (
        "Then the code is spent",
        "And a session cookie is set",
        "But no second cookie is set",
    )


def test_an_and_after_a_when_is_action_not_a_claim_and_a_later_then_opens_a_new_run() -> None:
    outline = _unit(
        BehaviorUnitKind.SCENARIO_OUTLINE,
        "Two actions, two claims",
        (
            ("When", "the user opens <path>"),
            ("And", "the page script runs"),
            ("Then", 'the browser lands on "/"'),
            ("When", "the user opens the URL again"),
            ("And", "nothing else happens"),
            ("Then", "no code is spent"),
        ),
    )

    assert unit_clauses(outline) == ('Then the browser lands on "/"', "Then no code is spent")


def test_a_scenario_without_a_then_has_no_clauses() -> None:
    scenario = _unit(BehaviorUnitKind.SCENARIO, "Only setup", (("Given", "a thing"), ("When", "it is poked")))

    assert unit_clauses(scenario) == ()
