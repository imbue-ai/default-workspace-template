from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr_behaviors.corpus import behavior_unit_matches_area
from imbue.mngr_behaviors.corpus import behavior_unit_matches_tag
from imbue.mngr_behaviors.corpus import binding_invariant_coordinates
from imbue.mngr_behaviors.corpus import scan_corpus
from imbue.mngr_behaviors.data_types import BehaviorUnit
from imbue.mngr_behaviors.data_types import BehaviorUnitKind
from imbue.mngr_behaviors.data_types import BehaviorViolation
from imbue.mngr_behaviors.data_types import CorpusScan
from imbue.mngr_mapreduce.utils import sanitize_for_agent_name
from imbue.mngr_witness.clauses import unit_clauses


class CorpusInvalidError(MngrError, RuntimeError):
    """Raised when the corpus has language violations; the pipeline never fans out over a broken corpus."""

    ...


class NoUnitsSelectedError(MngrError, RuntimeError):
    """Raised when the corpus and filters select no unit at all."""

    ...


class FeatureSlugCollisionError(MngrError, RuntimeError):
    """Raised when two selected feature files sanitize to the same slug, so their agents and branches would collide."""

    ...


class UnitSelection(FrozenModel):
    """Which units of the corpus an execution works on: every unit, or those matching the filters."""

    feature_paths: tuple[Path, ...] = Field(description="Root-relative feature files to keep; empty keeps every file")
    area: str | None = Field(description="Keep units under this folder area, or None")
    tag: str | None = Field(description="Keep units carrying this tag or coordinate, or None")
    unit_kind: BehaviorUnitKind | None = Field(description="Keep units of this kind, or None")


class UnitView(FrozenModel):
    """One behavior unit as the prompts and gates see it: its identity, its clauses, and its binding invariants."""

    coordinate: str = Field(description="The unit's coordinate")
    kind: BehaviorUnitKind = Field(description="Scenario, scenario outline, or rule")
    name: str = Field(description="The unit's name as written")
    line: int = Field(description="The line of its header in the feature file")
    parent: str | None = Field(description="The enclosing Rule's coordinate, if any")
    steps: tuple[str, ...] = Field(description="Every step as written, keyword included")
    clauses: tuple[str, ...] = Field(
        description="The unit's observable claims: its Then/And/But steps, or a Rule's name"
    )
    invariants: tuple[str, ...] = Field(description="Coordinates of the Rules in scope for the unit")


class FeatureTask(FrozenModel):
    """One feature file's worth of units: the unit of fan-out for the map stage and the unit of review after it."""

    relative_path: Path = Field(description="The feature file, relative to the corpus root")
    slug: str = Field(description="The folder-qualified, agent-name-safe slug of the file's agents and branches")
    units: tuple[UnitView, ...] = Field(description="The selected units of the file, in document order")

    @property
    def coordinates(self) -> tuple[str, ...]:
        return tuple(unit.coordinate for unit in self.units)


@pure
def format_corpus_violation(violation: BehaviorViolation) -> str:
    location = str(violation.file) if violation.line is None else f"{violation.file}:{violation.line}"
    return f"{location}: {violation.message}"


def scan_valid_corpus(corpus_root: Path) -> CorpusScan:
    """Scan the corpus and refuse to continue on any language violation."""
    scan = scan_corpus(corpus_root)
    if scan.violations:
        formatted = "\n".join(format_corpus_violation(violation) for violation in scan.violations)
        raise CorpusInvalidError(
            f"The behavior corpus at {corpus_root} has language violations; fix them (see `mngr behaviors validate`) "
            f"before fanning out:\n{formatted}"
        )
    return scan


@pure
def feature_task_slug(relative_path: Path) -> str:
    """``browser-authorization-signin`` for ``browser-authorization/signin.feature``.

    Already in the shape the executor sanitizes slugs into, so the agent and
    branch names it derives are the ones the prompts predict.
    """
    return sanitize_for_agent_name("-".join((*relative_path.parent.parts, relative_path.stem)))


@pure
def _is_unit_selected(unit: BehaviorUnit, corpus_root: Path, selection: UnitSelection) -> bool:
    relative_path = unit.file.relative_to(corpus_root)
    if selection.feature_paths and relative_path not in selection.feature_paths:
        return False
    if selection.area is not None and not behavior_unit_matches_area(unit, selection.area, corpus_root):
        return False
    if selection.tag is not None and not behavior_unit_matches_tag(unit, selection.tag):
        return False
    if selection.unit_kind is not None and unit.kind != selection.unit_kind:
        return False
    return True


@pure
def view_unit(unit: BehaviorUnit, scan: CorpusScan, corpus_root: Path) -> UnitView:
    return UnitView(
        coordinate=unit.coordinate,
        kind=unit.kind,
        name=unit.name,
        line=unit.line,
        parent=unit.parent,
        steps=tuple(f"{step.keyword} {step.text}" for step in unit.steps),
        clauses=unit_clauses(unit),
        invariants=binding_invariant_coordinates(unit, scan.units, corpus_root),
    )


@pure
def discover_feature_tasks(scan: CorpusScan, corpus_root: Path, selection: UnitSelection) -> list[FeatureTask]:
    """Group the selected units by feature file, in corpus scan order; raises NoUnitsSelectedError when none is selected."""
    units_by_relative_path: dict[Path, list[UnitView]] = {}
    for unit in scan.units:
        if _is_unit_selected(unit, corpus_root, selection):
            units_by_relative_path.setdefault(unit.file.relative_to(corpus_root), []).append(
                view_unit(unit, scan, corpus_root)
            )
    if not units_by_relative_path:
        raise NoUnitsSelectedError(f"No behavior units selected from the corpus at {corpus_root} with {selection}")
    tasks = [
        FeatureTask(relative_path=relative_path, slug=feature_task_slug(relative_path), units=tuple(units))
        for relative_path, units in units_by_relative_path.items()
    ]
    paths_by_slug: dict[str, list[Path]] = {}
    for task in tasks:
        paths_by_slug.setdefault(task.slug, []).append(task.relative_path)
    collisions = {slug: paths for slug, paths in paths_by_slug.items() if len(paths) > 1}
    if collisions:
        raise FeatureSlugCollisionError(f"Feature files whose slugs collide: {collisions}")
    return tasks
