"""Unit tests for the pure witness join/coverage/render logic (no subprocesses)."""

from pathlib import Path

from inline_snapshot import snapshot

from imbue.mngr_behaviors.data_types import BehaviorCoverage
from imbue.mngr_behaviors.data_types import BehaviorUnit
from imbue.mngr_behaviors.data_types import BehaviorUnitKind
from imbue.mngr_behaviors.data_types import WitnessLink
from imbue.mngr_behaviors.witnesses import behavior_coverage_record_value
from imbue.mngr_behaviors.witnesses import compute_behavior_coverage
from imbue.mngr_behaviors.witnesses import find_broken_witness_links
from imbue.mngr_behaviors.witnesses import group_witness_links_by_coordinate
from imbue.mngr_behaviors.witnesses import render_broken_witness_link_diagnostic
from imbue.mngr_behaviors.witnesses import render_matrix_record


def _scenario_unit(coordinate: str) -> BehaviorUnit:
    return BehaviorUnit(
        coordinate=coordinate,
        kind=BehaviorUnitKind.SCENARIO,
        name="Opening a fresh login URL signs the user in",
        file=Path("apps/minds/behaviors/browser-authorization/signin.feature"),
        line=4,
        tags=(coordinate.rsplit(".", 1)[-1],),
        steps=(),
        parent=None,
    )


def _link(test: str, coordinate: str | None, partial: str | None) -> WitnessLink:
    return WitnessLink(test=test, coordinate=coordinate, partial=partial)


def test_compute_behavior_coverage_is_none_without_any_links() -> None:
    assert compute_behavior_coverage(()) == BehaviorCoverage.NONE


def test_compute_behavior_coverage_is_full_when_a_link_has_no_partial_note() -> None:
    links = (_link("t.py::test_a", "browser-authorization.fresh-code", None),)

    assert compute_behavior_coverage(links) == BehaviorCoverage.FULL


def test_compute_behavior_coverage_is_partial_when_every_link_carries_a_partial_note() -> None:
    links = (
        _link("t.py::test_a", "browser-authorization.fresh-code", "skips the refusal path"),
        _link("t.py::test_b", "browser-authorization.fresh-code", "only the happy path"),
    )

    assert compute_behavior_coverage(links) == BehaviorCoverage.PARTIAL


def test_compute_behavior_coverage_is_full_when_full_and_partial_links_mix() -> None:
    links = (
        _link("t.py::test_a", "browser-authorization.fresh-code", "only the happy path"),
        _link("t.py::test_b", "browser-authorization.fresh-code", None),
    )

    assert compute_behavior_coverage(links) == BehaviorCoverage.FULL


def test_group_witness_links_by_coordinate_preserves_order_and_drops_uncoordinated() -> None:
    links = (
        _link("t.py::test_a", "browser-authorization.fresh-code", None),
        _link("t.py::test_invalid", None, None),
        _link("t.py::test_b", "browser-authorization.fresh-code", "partial b"),
        _link("t.py::test_c", "browser-authorization.session", None),
    )

    grouped = group_witness_links_by_coordinate(links)

    assert set(grouped) == {"browser-authorization.fresh-code", "browser-authorization.session"}
    assert [link.test for link in grouped["browser-authorization.fresh-code"]] == ["t.py::test_a", "t.py::test_b"]
    assert [link.test for link in grouped["browser-authorization.session"]] == ["t.py::test_c"]


def test_find_broken_witness_links_flags_dangling_and_invalid_in_collection_order() -> None:
    links = (
        _link("t.py::test_ok", "browser-authorization.fresh-code", None),
        _link("t.py::test_dangling", "browser-authorization.does-not-exist", None),
        _link("t.py::test_invalid", None, None),
    )
    unit_coordinates = frozenset({"browser-authorization.fresh-code"})

    broken_links = find_broken_witness_links(links, unit_coordinates)

    assert [link.test for link in broken_links] == ["t.py::test_dangling", "t.py::test_invalid"]


def test_find_broken_witness_links_is_empty_when_every_link_resolves() -> None:
    links = (_link("t.py::test_ok", "browser-authorization.fresh-code", None),)

    assert find_broken_witness_links(links, frozenset({"browser-authorization.fresh-code"})) == []


def test_behavior_coverage_record_value_spells_each_level_lowercase() -> None:
    spellings = {coverage: behavior_coverage_record_value(coverage) for coverage in BehaviorCoverage}

    assert spellings == snapshot(
        {
            BehaviorCoverage.FULL: "full",
            BehaviorCoverage.PARTIAL: "partial",
            BehaviorCoverage.NONE: "none",
        }
    )


def test_render_matrix_record_orders_fields_and_renders_full_coverage_with_witnesses() -> None:
    unit = _scenario_unit("browser-authorization.fresh-code")
    links = (
        _link("apps/minds/x_test.py::test_full", "browser-authorization.fresh-code", None),
        _link("apps/minds/x_test.py::test_partial", "browser-authorization.fresh-code", "skips the refusal path"),
    )

    record = render_matrix_record(unit, links)

    assert list(record.keys()) == ["coordinate", "kind", "name", "file", "line", "coverage", "witnesses"]
    assert record == snapshot(
        {
            "coordinate": "browser-authorization.fresh-code",
            "kind": "scenario",
            "name": "Opening a fresh login URL signs the user in",
            "file": "apps/minds/behaviors/browser-authorization/signin.feature",
            "line": 4,
            "coverage": "full",
            "witnesses": [
                {"test": "apps/minds/x_test.py::test_full", "partial": None},
                {"test": "apps/minds/x_test.py::test_partial", "partial": "skips the refusal path"},
            ],
        }
    )


def test_render_matrix_record_reports_none_coverage_with_no_witnesses() -> None:
    record = render_matrix_record(_scenario_unit("browser-authorization.session"), ())

    assert record["coverage"] == "none"
    assert record["witnesses"] == []


def test_render_broken_witness_link_diagnostic_distinguishes_dangling_from_invalid() -> None:
    dangling = _link("t.py::test_dangling", "browser-authorization.does-not-exist", None)
    invalid = _link("t.py::test_invalid", None, None)

    assert render_broken_witness_link_diagnostic(dangling) == snapshot(
        "t.py::test_dangling: witnesses coordinate 'browser-authorization.does-not-exist' matches no behavior unit"
    )
    assert render_broken_witness_link_diagnostic(invalid) == snapshot(
        "t.py::test_invalid: witnesses marker is missing a string coordinate argument"
    )
