"""Tests for the dashboards-as-code import flow."""

import json
from pathlib import Path

import pytest

from imbue.observability.dashboards import DashboardDefinitionError
from imbue.observability.dashboards import dashboard_definitions_dir
from imbue.observability.dashboards import ensure_dashboards
from imbue.observability.dashboards import load_dashboard_definitions
from imbue.observability.data_types import DashboardSummary
from imbue.observability.mock_openobserve_api_test import MockOpenObserveApi


def test_committed_dashboard_definitions_load_and_carry_panels() -> None:
    definitions = load_dashboard_definitions(dashboard_definitions_dir())

    assert len(definitions) >= 1
    for definition in definitions:
        tabs = definition.get("tabs")
        assert isinstance(tabs, list) and tabs, f"dashboard {definition.get('title')!r} has no tabs"
        first_tab = tabs[0]
        assert isinstance(first_tab, dict), f"dashboard {definition.get('title')!r} has a non-object first tab"
        # Read via items() rather than a keyed lookup: the type checker
        # narrows the untyped JSON dict's key type too far for .get().
        first_tab_panels = next((value for key, value in first_tab.items() if key == "panels"), None)
        assert first_tab_panels, f"dashboard {definition.get('title')!r} has no panels"


def test_load_dashboard_definitions_rejects_an_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(DashboardDefinitionError, match="No .*dashboard.json files"):
        load_dashboard_definitions(tmp_path)


def test_load_dashboard_definitions_rejects_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "broken.dashboard.json").write_text("{not json")

    with pytest.raises(DashboardDefinitionError, match="not valid JSON"):
        load_dashboard_definitions(tmp_path)


def test_load_dashboard_definitions_rejects_a_missing_title(tmp_path: Path) -> None:
    (tmp_path / "untitled.dashboard.json").write_text(json.dumps({"tabs": []}))

    with pytest.raises(DashboardDefinitionError, match="non-empty 'title'"):
        load_dashboard_definitions(tmp_path)


def test_load_dashboard_definitions_rejects_duplicate_titles(tmp_path: Path) -> None:
    (tmp_path / "first.dashboard.json").write_text(json.dumps({"title": "Fleet version mix", "tabs": []}))
    (tmp_path / "second.dashboard.json").write_text(json.dumps({"title": "Fleet version mix", "tabs": []}))

    with pytest.raises(DashboardDefinitionError, match="share the title"):
        load_dashboard_definitions(tmp_path)


def test_ensure_dashboards_creates_when_no_same_title_dashboard_exists() -> None:
    api = MockOpenObserveApi(
        dashboard_summaries=[DashboardSummary(dashboard_id="other-1", title="Something else")],
    )
    definition: dict[str, object] = {"title": "Fleet version mix", "tabs": []}

    actions = ensure_dashboards(api, [definition])

    assert api.deleted_dashboard_ids == []
    assert api.created_dashboards == [definition]
    assert len(actions) == 1
    assert actions[0].title == "Fleet version mix"
    assert actions[0].replaced_dashboard_ids == ()


def test_ensure_dashboards_replaces_every_same_title_dashboard() -> None:
    api = MockOpenObserveApi(
        dashboard_summaries=[
            DashboardSummary(dashboard_id="fleet-old-1", title="Fleet version mix"),
            DashboardSummary(dashboard_id="fleet-old-2", title="Fleet version mix"),
            DashboardSummary(dashboard_id="other-1", title="Something else"),
        ],
    )
    definition: dict[str, object] = {"title": "Fleet version mix", "tabs": []}

    actions = ensure_dashboards(api, [definition])

    assert api.deleted_dashboard_ids == ["fleet-old-1", "fleet-old-2"]
    assert api.created_dashboards == [definition]
    assert actions[0].replaced_dashboard_ids == ("fleet-old-1", "fleet-old-2")
