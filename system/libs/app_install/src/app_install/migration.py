"""Converting an app built before the self-contained layout into a user app with a record.

Such an app sits under ``system/apps/`` with a manifest and a hand-written program file. Its
routines are under ``system/routines/``, each naming the app in its ``app`` field, and its
manifest's ``[[references]]`` names what else in the workspace was built for it. Converting it
moves the routines into the app's directory, moves the references into its record, and replaces
its program file with the generated one. A skill a reference names stays where it is: a skill
joins an app only when the app is published.

A conversion either completes or changes nothing. It is planned in full before anything is
written, every step it then takes is undone if a later one fails, and an app whose existing
program file is not what its manifest generates is left as it is, with the reason, for an agent
to bring its manifest in line.
"""

import re
import shutil
import tomllib
from collections.abc import Callable
from enum import auto
from pathlib import Path
from typing import Final

import tomlkit
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import Field

from app_install.collisions import find_collisions
from app_install.derived import derive_files
from app_install.derived import describe_differences
from app_install.derived import program_names
from app_install.derived import write_derived_files
from app_install.errors import AppAlreadyRegisteredError
from app_install.errors import AppNotFoundError
from app_install.paths import APP_ROUTINES_DIRNAME
from app_install.paths import APPS_DIRECTORY
from app_install.paths import ROUTINES_DIRECTORY
from app_install.paths import app_directory
from app_install.paths import manifest_path
from app_install.paths import program_conf_path
from app_install.ports import allocate_port
from app_install.ports import local_port_of_row
from app_install.ports import read_registry_rows
from app_manifest.errors import AppManifestError
from app_manifest.manifest import AppManifest
from app_manifest.manifest import ManifestReadMode
from app_manifest.manifest import load_manifest
from app_manifest.manifest import scaffold_env_prefix
from app_manifest.records import AppOrigin
from app_manifest.records import AppRecord
from app_manifest.records import RecordReference
from app_manifest.records import load_all_records
from app_manifest.records import record_path
from app_manifest.records import write_record

_REGISTERED_URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"--url\s+http://(?:localhost|127\.0\.0\.1):(\d+)")
_COMMENT_PREFIXES: Final[tuple[str, ...]] = ("#", ";")


class _UndoKind(UpperCaseStrEnum):
    MOVE_BACK = auto()
    RESTORE_TEXT = auto()
    DELETE = auto()
    REMOVE_EMPTY_DIRECTORY = auto()


class _UndoStep(FrozenModel):
    """How to take back one step of a conversion."""

    kind: _UndoKind = Field(description="What undoing the step does")
    path: Path = Field(description="The path the step left changed")
    original_path: Path | None = Field(default=None, description="Where a moved path came from")
    original_text: str | None = Field(default=None, description="What a rewritten file held")


class MigrationPlan(FrozenModel):
    """Everything converting one app will do, or why it cannot be converted."""

    name: str = Field(description="The app's name")
    package: str = Field(description="The app's directory under system/apps/")
    port: int = Field(description="The port the app keeps")
    moves: tuple[tuple[str, str], ...] = Field(
        description="Each routine file that moves into the app, as (from, to), repo-relative"
    )
    references: tuple[RecordReference, ...] = Field(description="What the app's record lists as using the app")
    scope_exclude: tuple[str, ...] = Field(description="The manifest's scope exclusions, relative to the app")
    notes: tuple[str, ...] = Field(description="What the conversion drops or leaves for a person to look at")
    problems: tuple[str, ...] = Field(description="Why the app cannot be converted as it is; empty when it can")


class MigrateOutcome(FrozenModel):
    """What a conversion did."""

    plan: MigrationPlan = Field(description="What was planned")
    is_converted: bool = Field(description="Whether the app is now a user app with a record")
    problems: tuple[str, ...] = Field(description="Why it was not converted; empty when it was")


def _own_port(repo_root: Path, manifest: AppManifest) -> int | None:
    """The port the app already runs on: the one its program registers, else the one its registry row names."""
    conf = program_conf_path(repo_root, str(manifest.program))
    if conf.is_file():
        match = _REGISTERED_URL_PATTERN.search(conf.read_text(encoding="utf-8"))
        if match is not None:
            return int(match.group(1))
    for row in read_registry_rows(repo_root):
        port = local_port_of_row(row) if row.get("name") == str(manifest.name) else None
        if port is not None:
            return port
    return None


def _routine_files_of(repo_root: Path, name: str) -> list[Path]:
    routines: list[Path] = []
    for path in sorted((repo_root / ROUTINES_DIRECTORY).glob("*.toml")):
        try:
            owner = tomllib.loads(path.read_text(encoding="utf-8")).get("app")
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            logger.debug("Not moving the routine {}, which cannot be read: {}", path, e)
            continue
        if owner == name:
            routines.append(path)
    return routines


def _meaningful_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith(_COMMENT_PREFIXES)]


def _program_file_problems(repo_root: Path, record: AppRecord, manifest: AppManifest) -> list[str]:
    """Why each existing program file of the app cannot be replaced by the generated one; empty when all can: the
    two must say the same thing, apart from comments and the port the main program's generated file passes in
    the environment, which an app built before the port left its code already has as its default."""
    generated_by_path = derive_files(repo_root, record, manifest).text_by_path
    main_program = str(manifest.program)
    conventional_environment = f'environment={scaffold_env_prefix(manifest.name)}_PORT="{record.port}"'
    problems: list[str] = []
    for program in program_names(manifest):
        conf = program_conf_path(repo_root, program)
        if not conf.is_file():
            continue
        relative_conf = conf.relative_to(repo_root).as_posix()
        is_port_passed_by_convention = program == main_program and manifest.run is None
        generated_lines = [
            line
            for line in _meaningful_lines(generated_by_path[relative_conf])
            if not (is_port_passed_by_convention and line == conventional_environment)
        ]
        existing_lines = _meaningful_lines(conf.read_text(encoding="utf-8"))
        if existing_lines == generated_lines:
            continue
        differing = next(
            (
                existing
                for existing, expected in zip(existing_lines, generated_lines, strict=False)
                if existing != expected
            ),
            "a line the other does not have",
        )
        problems.append(
            f"{relative_conf} is not what the app's manifest generates, so it is kept as it is; say how the app "
            "runs in app.toml ([run], [[services]], [[secrets]]) and convert it again. It first differs at: "
            f"{differing}"
        )
    return problems


def plan_migration(repo_root: Path, package: str, is_bound: Callable[[int], bool]) -> MigrationPlan:
    """What converting the app at ``system/apps/<package>`` would do.

    Raises AppNotFoundError for a directory with no manifest and AppAlreadyRegisteredError for an app that
    already has a record.
    """
    path = manifest_path(repo_root, package)
    if not path.is_file():
        raise AppNotFoundError(f"no app at {app_directory(repo_root, package).relative_to(repo_root).as_posix()}")
    records = load_all_records(repo_root)
    if any(record.package == package for record in records):
        raise AppAlreadyRegisteredError(f"the app at system/apps/{package} already has a record")
    # An app built before today's template may carry a key this one no longer knows.
    manifest = load_manifest(path, repo_root=repo_root, mode=ManifestReadMode.TOLERANT)
    name = str(manifest.name)
    port = _own_port(repo_root, manifest)
    if port is None:
        port = allocate_port(repo_root, records, None, is_bound)
    record = AppRecord(name=manifest.name, package=package, port=port, origin=AppOrigin.LOCAL)
    app_relative = (APPS_DIRECTORY / package).as_posix()
    # The program files the app was given by hand are the app's own; the generated ones replace them.
    own_program_files = frozenset(
        program_conf_path(repo_root, program).relative_to(repo_root).as_posix() for program in program_names(manifest)
    )
    problems: list[str] = list(find_collisions(repo_root, package, manifest, own_program_files))
    notes: list[str] = []
    if manifest.wiring.programs:
        problems.append(
            f"its manifest lists [wiring] programs {[str(program) for program in manifest.wiring.programs]}; "
            "declare each as a [[services]] entry and convert it again"
        )
    problems.extend(_program_file_problems(repo_root, record, manifest))

    references = tuple(
        RecordReference(path=str(reference.path), note=str(reference.note or "")) for reference in manifest.references
    )
    moves: list[tuple[str, str]] = []
    for routine in _routine_files_of(repo_root, name):
        target = f"{app_relative}/{APP_ROUTINES_DIRNAME}/{routine.name}"
        if (repo_root / target).exists():
            problems.append(f"{target} already exists, so the routine {routine.stem} cannot move into the app")
            continue
        moves.append((routine.relative_to(repo_root).as_posix(), target))

    scope_exclude: list[str] = []
    for glob in (str(glob) for glob in manifest.scope.exclude):
        if glob.startswith(f"{app_relative}/"):
            scope_exclude.append(glob.removeprefix(f"{app_relative}/"))
        else:
            notes.append(f"the scope exclusion {glob!r} names nothing inside the app and is dropped")
    return MigrationPlan(
        name=name,
        package=package,
        port=port,
        moves=tuple(moves),
        references=references,
        scope_exclude=tuple(scope_exclude),
        notes=tuple(notes),
        problems=tuple(problems),
    )


def _rewritten_manifest_text(text: str, scope_exclude: tuple[str, ...]) -> str:
    """The manifest without the tables a user app no longer declares, its comments kept."""
    document = tomlkit.parse(text)
    for table in ("references", "wiring"):
        if table in document:
            del document[table]
    if "scope" in document:
        if scope_exclude:
            document["scope"] = {"exclude": list(scope_exclude)}
        else:
            del document["scope"]
    return document.as_string()


def _undo(steps: list[_UndoStep]) -> None:
    for step in reversed(steps):
        if step.kind is _UndoKind.MOVE_BACK and step.original_path is not None:
            shutil.move(step.path, step.original_path)
        elif step.kind is _UndoKind.RESTORE_TEXT and step.original_text is not None:
            step.path.write_text(step.original_text, encoding="utf-8")
        elif step.kind is _UndoKind.DELETE:
            if step.path.is_symlink() or step.path.is_file():
                step.path.unlink()
        elif step.kind is _UndoKind.REMOVE_EMPTY_DIRECTORY:
            if step.path.is_dir() and not any(step.path.iterdir()):
                step.path.rmdir()
        else:
            logger.warning("An undo step of kind {} for {} had nothing to restore", step.kind, step.path)


def _apply(repo_root: Path, plan: MigrationPlan, steps: list[_UndoStep]) -> tuple[str, ...]:
    """Carry the plan out, recording how to take each step back; returns what is wrong with the result."""
    for source, target in plan.moves:
        target_path = repo_root / target
        if not target_path.parent.is_dir():
            steps.append(_UndoStep(kind=_UndoKind.REMOVE_EMPTY_DIRECTORY, path=target_path.parent))
            target_path.parent.mkdir(parents=True)
        shutil.move(repo_root / source, target_path)
        steps.append(_UndoStep(kind=_UndoKind.MOVE_BACK, path=target_path, original_path=repo_root / source))

    manifest_file = manifest_path(repo_root, plan.package)
    manifest_text = manifest_file.read_text(encoding="utf-8")
    steps.append(_UndoStep(kind=_UndoKind.RESTORE_TEXT, path=manifest_file, original_text=manifest_text))
    manifest_file.write_text(_rewritten_manifest_text(manifest_text, plan.scope_exclude), encoding="utf-8")
    manifest = load_manifest(manifest_file, repo_root=repo_root, mode=ManifestReadMode.TOLERANT)

    record = AppRecord(
        name=manifest.name, package=plan.package, port=plan.port, origin=AppOrigin.LOCAL, references=plan.references
    )
    record_file = record_path(repo_root, plan.name)
    if not record_file.parent.is_dir():
        steps.append(_UndoStep(kind=_UndoKind.REMOVE_EMPTY_DIRECTORY, path=record_file.parent))
    steps.append(_UndoStep(kind=_UndoKind.DELETE, path=record_file))
    write_record(repo_root, record)

    derived = derive_files(repo_root, record, manifest)
    for relative_path in sorted(set(derived.text_by_path) | set(derived.link_target_by_path)):
        path = repo_root / relative_path
        if path.is_file() and not path.is_symlink():
            steps.append(
                _UndoStep(kind=_UndoKind.RESTORE_TEXT, path=path, original_text=path.read_text(encoding="utf-8"))
            )
        else:
            steps.append(_UndoStep(kind=_UndoKind.DELETE, path=path))
    write_derived_files(repo_root, plan.package, derived)
    return (
        *find_collisions(repo_root, plan.package, manifest, frozenset()),
        *describe_differences(repo_root, plan.package, derived),
    )


def migrate_app(repo_root: Path, package: str, is_bound: Callable[[int], bool], is_dry_run: bool) -> MigrateOutcome:
    """Convert the app at ``system/apps/<package>`` into a user app, or leave it exactly as it was.

    Raises AppNotFoundError for a directory with no manifest and AppAlreadyRegisteredError for an app that
    already has a record; anything else that keeps the app from converting is in the outcome's problems.
    """
    plan = plan_migration(repo_root, package, is_bound)
    if plan.problems or is_dry_run:
        return MigrateOutcome(plan=plan, is_converted=False, problems=plan.problems)
    steps: list[_UndoStep] = []
    try:
        problems = _apply(repo_root, plan, steps)
    except (OSError, AppManifestError) as e:
        _undo(steps)
        return MigrateOutcome(plan=plan, is_converted=False, problems=(f"the conversion failed part-way: {e}",))
    if problems:
        _undo(steps)
        return MigrateOutcome(plan=plan, is_converted=False, problems=problems)
    return MigrateOutcome(plan=plan, is_converted=True, problems=())
