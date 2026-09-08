"""Dashboards-as-code: import the committed dashboard definitions into an instance.

The committed ``*.dashboard.json`` files under ``dashboards/`` (next to this
module) are the source of truth; the instance's copy is disposable. Import is
therefore replace-by-title: any existing dashboard with the same title is
deleted and the committed definition created fresh, so re-running always
converges on exactly what the repo holds (hand edits in the UI are for
iterating -- export them back into the repo to keep them).
"""

import json
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.observability.errors import ObservabilityError
from imbue.observability.openobserve_api import OpenObserveApiInterface

_DASHBOARD_DEFINITIONS_DIR_NAME = "dashboards"
_DASHBOARD_FILE_SUFFIX = ".dashboard.json"


class DashboardDefinitionError(ObservabilityError):
    """Raised when a committed dashboard definition file is missing or malformed."""


class DashboardImportAction(FrozenModel):
    """What the import did for one committed definition."""

    title: str = Field(description="The dashboard's title (the replace-by-title match key)")
    replaced_dashboard_ids: tuple[str, ...] = Field(description="Ids of same-title dashboards deleted before create")


@pure
def dashboard_definitions_dir() -> Path:
    return Path(__file__).parent / _DASHBOARD_DEFINITIONS_DIR_NAME


def load_dashboard_definitions(definitions_dir: Path) -> list[dict[str, object]]:
    """Parse every committed ``*.dashboard.json`` under ``definitions_dir``.

    Raises ``DashboardDefinitionError`` when the directory holds no
    definitions, any file is not a JSON object with a non-empty title, or two
    files share a title -- an import that silently did nothing (or created an
    untitled dashboard that replace-by-title can never match again) helps nobody,
    and a duplicated title would make the replace-by-title import churn (or
    corrupt) the same dashboard from two sources.
    """
    definition_paths = sorted(definitions_dir.glob(f"*{_DASHBOARD_FILE_SUFFIX}"))
    if not definition_paths:
        raise DashboardDefinitionError(f"No {_DASHBOARD_FILE_SUFFIX} files found under {definitions_dir}")
    definitions: list[dict[str, object]] = []
    first_path_by_title: dict[str, Path] = {}
    for definition_path in definition_paths:
        try:
            parsed = json.loads(definition_path.read_text())
        except json.JSONDecodeError as e:
            raise DashboardDefinitionError(f"Dashboard definition {definition_path} is not valid JSON") from e
        if not isinstance(parsed, dict) or not str(parsed.get("title", "")).strip():
            raise DashboardDefinitionError(
                f"Dashboard definition {definition_path} must be a JSON object with a non-empty 'title'"
            )
        title = str(parsed["title"])
        first_path_with_title = first_path_by_title.get(title)
        if first_path_with_title is not None:
            raise DashboardDefinitionError(
                f"Dashboard definitions {first_path_with_title} and {definition_path} share the title"
                f" {title!r}; replace-by-title import needs unique titles"
            )
        first_path_by_title[title] = definition_path
        definitions.append(parsed)
    return definitions


def ensure_dashboards(
    api: OpenObserveApiInterface,
    definitions: list[dict[str, object]],
) -> list[DashboardImportAction]:
    """Replace-by-title import of every definition; returns what happened per dashboard."""
    existing_summaries = api.list_dashboard_summaries()
    actions: list[DashboardImportAction] = []
    for definition in definitions:
        title = str(definition["title"])
        matching_ids = tuple(
            summary.dashboard_id for summary in existing_summaries if summary.title == title and summary.dashboard_id
        )
        for dashboard_id in matching_ids:
            api.delete_dashboard(dashboard_id)
        api.create_dashboard(definition)
        logger.info("Imported dashboard {} (replaced {} existing)", title, len(matching_ids))
        actions.append(DashboardImportAction(title=title, replaced_dashboard_ids=matching_ids))
    return actions
