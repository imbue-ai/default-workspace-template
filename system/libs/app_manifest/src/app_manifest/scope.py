import configparser
import json
import subprocess
import time
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Final

import pathspec
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

from app_manifest.errors import ManifestLoadError
from app_manifest.errors import ScopeComputationError
from app_manifest.manifest import APPS_DIRECTORY_PARTS
from app_manifest.manifest import MANIFEST_FILENAME
from app_manifest.manifest import AppManifest
from app_manifest.manifest import AppReference
from app_manifest.manifest import app_package_directory
from app_manifest.manifest import load_manifest
from app_manifest.primitives import ExcludeGlob
from app_manifest.primitives import ReferenceNote
from app_manifest.primitives import ReferencePath
from app_manifest.primitives import RepoRelativePath
from app_manifest.primitives import is_path_covered_by

# The wiring file every supervised app has a block in.
_SUPERVISORD_CONF: Final[RepoRelativePath] = RepoRelativePath("system/supervisord.conf")

# Applied to every footprint and never written in a manifest: vendored subtrees, gitignored
# runtime state, installed dependencies, and build output are nobody's creation.
BUILT_IN_EXCLUDES: Final[tuple[ExcludeGlob, ...]] = (
    ExcludeGlob("system/vendor/**"),
    ExcludeGlob("data/**"),
    ExcludeGlob("**/node_modules/**"),
    ExcludeGlob("**/dist/**"),
    ExcludeGlob("**/.venv/**"),
)

APP_CONVENTIONS: Final[tuple[RepoRelativePath, ...]] = (
    RepoRelativePath("system/apps/README.md"),
    RepoRelativePath(".agents/shared/worker/references/type-app.md"),
    RepoRelativePath("docs/system/style_guide.md"),
)

SKILL_CONVENTIONS: Final[tuple[RepoRelativePath, ...]] = (
    RepoRelativePath(".agents/shared/worker/references/type-skill.md"),
    RepoRelativePath(".agents/shared/references/spec-summary.md"),
    RepoRelativePath("docs/system/style_guide.md"),
)

# A git command that has not finished by the hard timeout is broken rather than slow; one that
# takes longer than the warning threshold is a repository worth looking at before it breaks.
_GIT_HARD_TIMEOUT_SECONDS: Final[float] = 60.0
_GIT_SLOW_THRESHOLD_SECONDS: Final[float] = 15.0

# pathspec's pattern factory for gitignore syntax, which is what an exclude glob is written in.
_EXCLUDE_PATTERN_STYLE: Final[str] = "gitignore"


class CreationType(LowerCaseStrEnum):
    """What kind of creation a scope file describes."""

    APP = auto()
    SKILL = auto()


class ReferenceKind(LowerCaseStrEnum):
    """What sort of artifact a referenced path is, derived from where it sits in the tree."""

    SKILL = auto()
    SHARED = auto()
    SCRIPT = auto()
    SERVICE = auto()
    DOC = auto()
    OTHER = auto()


_REFERENCE_KIND_BY_PREFIX: Final[tuple[tuple[str, ReferenceKind], ...]] = (
    (".agents/skills/", ReferenceKind.SKILL),
    (".agents/shared/", ReferenceKind.SHARED),
    ("system/scripts/", ReferenceKind.SCRIPT),
    ("system/services/", ReferenceKind.SERVICE),
    ("docs/", ReferenceKind.DOC),
)


class CreationIdentity(FrozenModel):
    """Which creation a scope file is about."""

    type: CreationType = Field(description="app or skill")
    name: NonEmptyStr = Field(description="The app's registered name, or the skill's directory name")
    package: NonEmptyStr | None = Field(description="The app's package directory name; absent for a skill")
    manifest: RepoRelativePath | None = Field(description="The app's manifest; absent for a skill")


class WiringSection(FrozenModel):
    """A shared configuration file and the blocks in it that belong to one creation."""

    path: RepoRelativePath = Field(description="The configuration file")
    sections: tuple[NonEmptyStr, ...] = Field(description="The section names the creation owns, in file order")


class ScopeReference(FrozenModel):
    """One referenced artifact as a scope file carries it."""

    path: ReferencePath = Field(description="The literal repo-root-relative file or directory")
    note: ReferenceNote | None = Field(description="The manifest's note, when it wrote one")
    kind: ReferenceKind = Field(description="What sort of artifact this is, from its prefix")


class DiffSummary(FrozenModel):
    """A branch's changed files, and the ones the footprint does not account for."""

    base: NonEmptyStr = Field(description="The full sha the diff was taken against")
    files: tuple[RepoRelativePath, ...] = Field(description="Every file the diff changed")
    outside_footprint: tuple[RepoRelativePath, ...] = Field(
        description="The changed files that are neither in the footprint nor excluded"
    )


class CreationScope(FrozenModel):
    """One creation's footprint: what a review, test, freshness, or publish pass treats as its own."""

    creation: CreationIdentity = Field(description="Which creation this is")
    primary: tuple[RepoRelativePath, ...] = Field(description="The creation's own directories and files")
    wiring: tuple[WiringSection, ...] = Field(description="The shared config blocks the creation owns")
    references: tuple[ScopeReference, ...] = Field(description="The artifacts the manifest declares it owns")
    context: tuple[RepoRelativePath, ...] = Field(description="Read-only surfaces the creation is judged against")
    conventions: tuple[RepoRelativePath, ...] = Field(description="The convention docs for this creation type")
    exclude: tuple[ExcludeGlob, ...] = Field(description="Globs no pass considers, built-ins first")
    diff: DiffSummary | None = Field(description="The diff against a base, when one was asked for")


class ManifestReferenceMatch(FrozenModel):
    """A manifest whose reference covers some target path, and the reference that covers it."""

    manifest_path: Path = Field(description="The absolute path of the matching app.toml")
    manifest: AppManifest = Field(description="The matching manifest")
    reference: AppReference = Field(description="The reference entry that covers the target")


@pure
def reference_kind_for_path(path: ReferencePath) -> ReferenceKind:
    for prefix, kind in _REFERENCE_KIND_BY_PREFIX:
        if path.startswith(prefix):
            return kind
    return ReferenceKind.OTHER


@pure
def _deduplicated(globs: Sequence[ExcludeGlob]) -> tuple[ExcludeGlob, ...]:
    """The globs in the order given, with every repeat after the first dropped."""
    return tuple(dict.fromkeys(globs))


def find_wiring_sections(repo_root: Path, manifest: AppManifest) -> tuple[WiringSection, ...]:
    """The supervisord blocks that run the app: its own program, plus every ``<app name>-<role>`` sidecar.

    A conf with none of them (an app that is not registered yet) yields no wiring rather than an error.
    """
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(repo_root / _SUPERVISORD_CONF)
    except configparser.Error as e:
        raise ScopeComputationError(f"cannot parse {repo_root / _SUPERVISORD_CONF}: {e}") from e
    own_section = f"program:{manifest.program}"
    # The sidecar rule is a prefix match, so it assumes no unrelated program is named with
    # the app's name plus a hyphen: an app called "share" would claim a "share-gateway"
    # program that is nobody's sidecar.
    sidecar_prefix = f"program:{manifest.name}-"
    owned_sections = tuple(
        NonEmptyStr(section)
        for section in parser.sections()
        if section == own_section or section.startswith(sidecar_prefix)
    )
    if not owned_sections:
        return ()
    return (WiringSection(path=_SUPERVISORD_CONF, sections=owned_sections),)


def find_referencing_manifests(
    repo_root: Path, target_path: RepoRelativePath
) -> tuple[ManifestReferenceMatch, ...]:
    """Every app whose manifest declares ``target_path`` as its own, in manifest path order.

    An app directory with no ``app.toml`` is skipped, and so is a manifest that fails to load:
    the loud check for a broken manifest is ``system/test_app_manifests.py``, and one app's
    stale reference must not block every other creation's footprint and freshness check.
    """
    normalized_target = target_path.rstrip("/")
    apps_directory = repo_root.joinpath(*APPS_DIRECTORY_PARTS)
    matches: list[ManifestReferenceMatch] = []
    for manifest_path in sorted(apps_directory.glob(f"*/{MANIFEST_FILENAME}")):
        try:
            manifest = load_manifest(manifest_path, repo_root=repo_root)
        except ManifestLoadError as e:
            logger.warning(
                "Skipping {} while looking up what owns {}, because it does not load: {}",
                manifest_path,
                normalized_target,
                e,
            )
            continue
        for reference in manifest.references:
            if is_path_covered_by(reference.path, normalized_target):
                matches.append(
                    ManifestReferenceMatch(
                        manifest_path=manifest_path, manifest=manifest, reference=reference
                    )
                )
                break
    return tuple(matches)


def compute_app_scope(repo_root: Path, manifest_path: Path, manifest: AppManifest) -> CreationScope:
    """The footprint of the app a manifest describes, with no diff attached yet."""
    package_directory = app_package_directory(repo_root, manifest_path)
    if package_directory is None:
        raise ScopeComputationError(f"manifest {manifest_path} is not inside the repo root {repo_root}")
    resolved_manifest_path = manifest_path.resolve()
    return CreationScope(
        creation=CreationIdentity(
            type=CreationType.APP,
            name=NonEmptyStr(manifest.name),
            package=NonEmptyStr(resolved_manifest_path.parent.name),
            manifest=RepoRelativePath(resolved_manifest_path.relative_to(repo_root).as_posix()),
        ),
        primary=(RepoRelativePath(package_directory),),
        wiring=find_wiring_sections(repo_root, manifest),
        references=tuple(
            ScopeReference(
                path=reference.path,
                note=reference.note,
                kind=reference_kind_for_path(reference.path),
            )
            for reference in manifest.references
        ),
        context=(),
        conventions=APP_CONVENTIONS,
        exclude=_deduplicated(BUILT_IN_EXCLUDES + manifest.scope.exclude),
        diff=None,
    )


def compute_skill_scope(repo_root: Path, target_path: RepoRelativePath) -> CreationScope:
    """The footprint of a skill (or any other non-app path), with the apps that own it as context.

    Raises ScopeComputationError when nothing is there: the freshness check feeds ``primary``
    into ``git diff -- <paths>``, which silently ignores a pathspec that matches nothing, so a
    mistyped path would otherwise read as a creation with no changes at all.
    """
    normalized_target = target_path.rstrip("/")
    target = repo_root / normalized_target
    if not target.exists():
        raise ScopeComputationError(
            f"{normalized_target!r} does not exist under {repo_root}, so a footprint over it would "
            "cover nothing rather than the creation it names"
        )
    primary_entry = f"{normalized_target}/" if target.is_dir() else normalized_target
    owning_directories = [
        app_package_directory(repo_root, match.manifest_path)
        for match in find_referencing_manifests(repo_root, target_path)
    ]
    return CreationScope(
        creation=CreationIdentity(
            type=CreationType.SKILL,
            name=NonEmptyStr(Path(normalized_target).name),
            package=None,
            manifest=None,
        ),
        primary=(RepoRelativePath(primary_entry),),
        wiring=(),
        references=(),
        context=tuple(
            RepoRelativePath(directory) for directory in owning_directories if directory is not None
        ),
        conventions=SKILL_CONVENTIONS,
        exclude=BUILT_IN_EXCLUDES,
        diff=None,
    )


@pure
def is_accounted_for_by_scope(
    candidate: RepoRelativePath, scope: CreationScope, exclude_spec: pathspec.PathSpec
) -> bool:
    """Whether a changed file sits inside the footprint or is excluded outright.

    Of each context directory only its manifest counts as inside: a skill's one sanctioned
    edit outside its own directory is the ``[[references]]`` entry it adds to the owning
    app's ``app.toml``; a change to the app's code stays outside the skill's footprint.
    """
    if exclude_spec.match_file(candidate):
        return True
    if any(wiring.path == candidate for wiring in scope.wiring):
        return True
    context_manifests = (f"{context.rstrip('/')}/{MANIFEST_FILENAME}" for context in scope.context)
    if candidate in context_manifests:
        return True
    owned_entries = (*scope.primary, *(reference.path for reference in scope.references))
    return any(is_path_covered_by(entry, candidate) for entry in owned_entries)


def _run_git(repo_root: Path, arguments: Sequence[str]) -> str:
    """Raises ScopeComputationError when git cannot be run, times out, or exits non-zero."""
    command = ("git", *arguments)
    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_GIT_HARD_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ScopeComputationError(f"cannot run {' '.join(command)} in {repo_root}: {e}") from e
    if completed.returncode != 0:
        raise ScopeComputationError(
            f"{' '.join(command)} failed in {repo_root} (exit {completed.returncode}): {completed.stderr.strip()}"
        )
    elapsed_seconds = time.monotonic() - started_at
    if elapsed_seconds > _GIT_SLOW_THRESHOLD_SECONDS:
        logger.warning(
            "Took {:.1f}s to run {} in {}, which is far slower than reading a diff should be",
            elapsed_seconds,
            " ".join(command),
            repo_root,
        )
    return completed.stdout


def with_diff_against_base(scope: CreationScope, repo_root: Path, diff_base: str) -> CreationScope:
    """The same scope with its diff filled in from the changed files between a base and HEAD."""
    base_sha = NonEmptyStr(_run_git(repo_root, ("rev-parse", f"{diff_base}^{{commit}}")).strip())
    # A NUL-separated listing with quoting off is the only form every filename survives: git
    # otherwise renders a non-ASCII name as an escaped, double-quoted string, which is not the
    # path it changed, and a name with a newline in it would split across lines.
    diff_output = _run_git(
        repo_root,
        ("-c", "core.quotePath=false", "diff", "--name-only", "-z", f"{base_sha}...HEAD"),
    )
    changed_files = tuple(
        RepoRelativePath(entry) for entry in diff_output.split("\0") if entry
    )
    exclude_spec = pathspec.PathSpec.from_lines(_EXCLUDE_PATTERN_STYLE, scope.exclude)
    outside_footprint = tuple(
        changed_file
        for changed_file in changed_files
        if not is_accounted_for_by_scope(changed_file, scope, exclude_spec)
    )
    diff_summary = DiffSummary(
        base=base_sha, files=changed_files, outside_footprint=outside_footprint
    )
    return scope.model_copy_update(to_update(scope.field_ref().diff, diff_summary))


@pure
def render_scope_file(scope: CreationScope) -> str:
    """The scope file's JSON text, indented and newline-terminated."""
    return f"{json.dumps(scope.model_dump(mode='json'), indent=2)}\n"
