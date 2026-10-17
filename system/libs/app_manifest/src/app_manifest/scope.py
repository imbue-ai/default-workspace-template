import configparser
import json
import re
import subprocess
import time
from collections.abc import Callable
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never

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

# The shared wiring files where supervised apps have their blocks: the main config and
# the drop-in directory its ``[include]`` glob names. The template declares its programs
# one per drop-in file, so a reader of the main config alone sees none of them.
# ``system/test_supervisord_layout.py`` pins the directory as the one the glob names.
_SUPERVISORD_CONF: Final[RepoRelativePath] = RepoRelativePath("system/supervisord.conf")
_SUPERVISORD_DROPIN_DIR: Final[RepoRelativePath] = RepoRelativePath(
    "system/supervisord.conf.d"
)

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

SERVICE_CONVENTIONS: Final[tuple[RepoRelativePath, ...]] = (
    RepoRelativePath("system/services/README.md"),
    RepoRelativePath(".agents/shared/worker/references/type-service.md"),
    RepoRelativePath("docs/system/style_guide.md"),
)

_SKILLS_DIRECTORY: Final[str] = ".agents/skills"
# One segment of a creation's name: a directory or file name, never "." or "..".
_CREATION_NAME_SEGMENT: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHARED_DIRECTORY: Final[str] = ".agents/shared"
_SERVICES_DIRECTORY: Final[str] = "system/services"

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
    SERVICE = auto()


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

    type: CreationType = Field(description="app, skill, or service")
    name: NonEmptyStr = Field(description="The app's registered name, or the skill's or service's directory name")
    package: NonEmptyStr | None = Field(
        description="The app's or service's package directory name; absent for a skill"
    )
    manifest: RepoRelativePath | None = Field(
        description="The app's manifest; absent for a skill, a service, or an app that has none"
    )


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
    """The files changed between a base and a ref, split by whether the footprint accounts for them."""

    base: NonEmptyStr = Field(
        description="The full sha of the requested base; the diff runs from its merge base with the ref"
    )
    ref: NonEmptyStr = Field(description="The full sha of the ref the diff runs to (HEAD unless asked otherwise)")
    files: tuple[RepoRelativePath, ...] = Field(description="Every file the diff changed")
    inside_footprint: tuple[RepoRelativePath, ...] = Field(
        description="The changed files the footprint accounts for and the excludes do not swallow"
    )
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
    diff: DiffSummary | None = Field(description="The diff against a base, when the caller asks for one")


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
    """The supervisord blocks that run the app: its own program, every ``<app name>-<role>``
    sidecar, and every program its manifest's ``[wiring] programs`` declares.

    Blocks are looked for in the main config and in every drop-in its ``[include]`` glob
    names, and each returned section is attributed to the file it is actually written in,
    so a footprint points at the file a change would have to edit.

    A conf with none of the derived ones (an app that is not registered yet) yields no wiring
    rather than an error; a declared program with no block is an error, since the declaration
    is explicit.
    """
    own_section = f"program:{manifest.program}"
    # The sidecar rule is a prefix match; the app-name rule reserves the first label of every
    # standalone program (`share` for `share-gateway`), which is what keeps an unrelated
    # program from being claimed here.
    sidecar_prefix = f"program:{manifest.name}-"
    declared_sections = [f"program:{program}" for program in manifest.wiring.programs]

    wiring, defined_sections = _owned_wiring_sections(
        repo_root,
        lambda section: section == own_section
        or section.startswith(sidecar_prefix)
        or section in declared_sections,
    )
    for declared_section in declared_sections:
        if declared_section not in defined_sections:
            raise ScopeComputationError(
                f"manifest {manifest.name} declares wiring program {declared_section!r}, which "
                f"{repo_root / _SUPERVISORD_CONF} and its drop-ins do not define"
            )
    return wiring


def _owned_wiring_sections(
    repo_root: Path, is_owned: Callable[[str], bool]
) -> tuple[tuple[WiringSection, ...], frozenset[str]]:
    """The sections ``is_owned`` accepts, attributed to the file each is written in, and
    every section the main config and its drop-ins define."""
    wiring: list[WiringSection] = []
    defined_sections: set[str] = set()
    for conf_path in _supervisord_conf_paths(repo_root):
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(repo_root / conf_path)
        except configparser.Error as e:
            raise ScopeComputationError(f"cannot parse {repo_root / conf_path}: {e}") from e
        sections = parser.sections()
        defined_sections.update(sections)
        owned_sections = tuple(NonEmptyStr(section) for section in sections if is_owned(section))
        if owned_sections:
            wiring.append(WiringSection(path=conf_path, sections=owned_sections))
    return tuple(wiring), frozenset(defined_sections)


@pure
def program_name_for_package(package: str) -> str:
    """The supervisord program a creation with no manifest runs as: its package directory's
    name with underscores as hyphens (``host_backup`` runs as ``host-backup``)."""
    return package.replace("_", "-")


def find_named_wiring_sections(repo_root: Path, program: str) -> tuple[WiringSection, ...]:
    """The supervisord blocks a creation with no manifest owns by name: the program named
    ``program`` and every ``<program>-<role>`` one."""
    own_section = f"program:{program}"
    wiring, _ = _owned_wiring_sections(
        repo_root,
        lambda section: section == own_section or section.startswith(f"{own_section}-"),
    )
    return wiring


def _supervisord_conf_paths(repo_root: Path) -> tuple[RepoRelativePath, ...]:
    """The main config followed by its drop-ins, in the order supervisord reads them."""
    dropins = sorted((repo_root / _SUPERVISORD_DROPIN_DIR).glob("*.conf"))
    return (
        _SUPERVISORD_CONF,
        *(
            RepoRelativePath(path.relative_to(repo_root).as_posix())
            for path in dropins
        ),
    )


class LoadedManifest(FrozenModel):
    """An app manifest that loaded, and where it was read from."""

    manifest_path: Path = Field(description="The absolute path of the app.toml")
    manifest: AppManifest = Field(description="The validated manifest")


def load_app_manifests(repo_root: Path) -> tuple[LoadedManifest, ...]:
    """Every app manifest under the repo root that loads, in manifest path order.

    An app directory with no ``app.toml`` is skipped, and so is a manifest that fails to load:
    the loud check for a broken manifest is ``system/test_app_manifests.py``, and one app's
    stale reference must not block every other creation's footprint and freshness check.
    """
    repo_root = repo_root.resolve()
    apps_directory = repo_root.joinpath(*APPS_DIRECTORY_PARTS)
    loaded: list[LoadedManifest] = []
    for manifest_path in sorted(apps_directory.glob(f"*/{MANIFEST_FILENAME}")):
        try:
            manifest = load_manifest(manifest_path, repo_root=repo_root)
        except ManifestLoadError as e:
            logger.warning("Skipping {}, because it does not load: {}", manifest_path, e)
            continue
        loaded.append(LoadedManifest(manifest_path=manifest_path, manifest=manifest))
    return tuple(loaded)


@pure
def match_referencing_manifests(
    manifests: Sequence[LoadedManifest], target_path: RepoRelativePath
) -> tuple[ManifestReferenceMatch, ...]:
    """Every one of ``manifests`` that declares ``target_path`` as its own, in the order given."""
    normalized_target = target_path.rstrip("/")
    matches: list[ManifestReferenceMatch] = []
    for loaded in manifests:
        for reference in loaded.manifest.references:
            if is_path_covered_by(reference.path, normalized_target):
                matches.append(
                    ManifestReferenceMatch(
                        manifest_path=loaded.manifest_path,
                        manifest=loaded.manifest,
                        reference=reference,
                    )
                )
                break
    return tuple(matches)


def find_referencing_manifests(
    repo_root: Path, target_path: RepoRelativePath
) -> tuple[ManifestReferenceMatch, ...]:
    """Every app whose manifest declares ``target_path`` as its own, in manifest path order.

    Manifests that do not load are skipped, as ``load_app_manifests`` describes.
    """
    return match_referencing_manifests(load_app_manifests(repo_root), target_path)


def _check_excludes_leave_the_footprint(
    repo_root: Path, exclude: Sequence[ExcludeGlob], footprint_entries: Sequence[RepoRelativePath]
) -> None:
    """Raises when the excludes swallow a footprint entry whole: a manifest that excludes its
    own directory or a reference would leave every pass blind to it with nothing in
    ``outside_footprint`` to say so. Carving a subdirectory out of an entry is the
    intended use and passes."""
    exclude_spec = pathspec.PathSpec.from_lines(_EXCLUDE_PATTERN_STYLE, exclude)
    for entry in footprint_entries:
        entry_path = repo_root / entry
        if entry_path.is_dir():
            files = [
                candidate.relative_to(repo_root).as_posix()
                for candidate in entry_path.rglob("*")
                if candidate.is_file()
            ]
        else:
            files = [entry]
        if files and all(exclude_spec.match_file(file) for file in files):
            raise ScopeComputationError(
                f"the exclude globs cover {str(entry)!r}, which is part of the footprint"
            )


def compute_app_scope(repo_root: Path, manifest_path: Path, manifest: AppManifest) -> CreationScope:
    """The footprint of the app a manifest describes, with no diff attached yet."""
    repo_root = repo_root.resolve()
    package_directory = app_package_directory(repo_root, manifest_path)
    if package_directory is None:
        raise ScopeComputationError(f"manifest {manifest_path} is not inside the repo root {repo_root}")
    resolved_manifest_path = manifest_path.resolve()
    primary = (RepoRelativePath(package_directory),)
    references = tuple(
        ScopeReference(
            path=reference.path,
            note=reference.note,
            kind=reference_kind_for_path(reference.path),
        )
        for reference in manifest.references
    )
    exclude = _deduplicated(BUILT_IN_EXCLUDES + manifest.scope.exclude)
    _check_excludes_leave_the_footprint(
        repo_root, exclude, (*primary, *(reference.path for reference in references))
    )
    return CreationScope(
        creation=CreationIdentity(
            type=CreationType.APP,
            name=NonEmptyStr(manifest.name),
            package=NonEmptyStr(resolved_manifest_path.parent.name),
            manifest=RepoRelativePath(resolved_manifest_path.relative_to(repo_root).as_posix()),
        ),
        primary=primary,
        wiring=find_wiring_sections(repo_root, manifest),
        references=references,
        context=(),
        conventions=APP_CONVENTIONS,
        exclude=exclude,
        diff=None,
    )


def compute_skill_scope(repo_root: Path, target_path: RepoRelativePath) -> CreationScope:
    """The footprint of a skill (or any other non-app path), with the apps that own it as context.

    Raises ScopeComputationError when nothing is there: the freshness check feeds ``primary``
    into ``git diff -- <paths>``, which silently ignores a pathspec that matches nothing, so a
    mistyped path would otherwise read as a creation with no changes at all.
    """
    repo_root = repo_root.resolve()
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
def is_inside_footprint(candidate: RepoRelativePath, scope: CreationScope) -> bool:
    """Whether a changed file sits inside the footprint.

    Of each context directory only its manifest counts as inside: a skill's one sanctioned
    edit outside its own directory is the ``[[references]]`` entry it adds to the owning
    app's ``app.toml``; a change to the app's code stays outside the skill's footprint.
    """
    if any(wiring.path == candidate for wiring in scope.wiring):
        return True
    if any(candidate == f"{context.rstrip('/')}/{MANIFEST_FILENAME}" for context in scope.context):
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


class ChangedFiles(FrozenModel):
    """The files a ref changed since it forked from a base, and the commits that bound the range."""

    base: NonEmptyStr = Field(description="The full sha of the requested base")
    ref: NonEmptyStr = Field(description="The full sha of the ref the diff runs to")
    merge_base: NonEmptyStr = Field(description="The full sha the diff actually runs from")
    files: tuple[RepoRelativePath, ...] = Field(description="Every file the diff changed")


def resolve_commit(repo_root: Path, ref: str) -> NonEmptyStr:
    """The full sha of the commit ``ref`` names."""
    return NonEmptyStr(_run_git(repo_root, ("rev-parse", f"{ref}^{{commit}}")).strip())


def list_changed_files(repo_root: Path, diff_base: str, diff_ref: str) -> ChangedFiles:
    """The files ``diff_ref`` changed since its merge base with ``diff_base`` (the three-dot
    form, so what the base branch did after the fork is not counted as the ref's change)."""
    base_sha = resolve_commit(repo_root, diff_base)
    ref_sha = resolve_commit(repo_root, diff_ref)
    merge_base_sha = NonEmptyStr(_run_git(repo_root, ("merge-base", base_sha, ref_sha)).strip())
    # A NUL-separated listing with quoting off is the only form every filename survives: git
    # otherwise renders a non-ASCII name as an escaped, double-quoted string, which is not the
    # path it changed, and a name with a newline in it would split across lines. With rename
    # detection a renamed file would list only its new path, though its old path is gone.
    diff_output = _run_git(
        repo_root,
        (
            "-c",
            "core.quotePath=false",
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            f"{base_sha}...{ref_sha}",
        ),
    )
    return ChangedFiles(
        base=base_sha,
        ref=ref_sha,
        merge_base=merge_base_sha,
        files=tuple(RepoRelativePath(entry) for entry in diff_output.split("\0") if entry),
    )


def list_tracked_files(repo_root: Path) -> tuple[RepoRelativePath, ...]:
    """Every file git tracks in the repo root's index, in git's order."""
    output = _run_git(repo_root, ("-c", "core.quotePath=false", "ls-files", "-z"))
    return tuple(RepoRelativePath(entry) for entry in output.split("\0") if entry)


def list_uncommitted_paths(repo_root: Path) -> tuple[RepoRelativePath, ...]:
    """Every path the working tree or index holds a change to that HEAD does not, untracked
    files included (ignored ones are not)."""
    output = _run_git(
        repo_root,
        (
            "-c",
            "core.quotePath=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--no-renames",
            "--untracked-files=all",
        ),
    )
    # Each entry is a two-letter status, a space, then the path.
    return tuple(RepoRelativePath(entry[3:]) for entry in output.split("\0") if entry)


def read_file_at_revision(repo_root: Path, revision: str, path: RepoRelativePath) -> str | None:
    """The text of ``path`` in the tree of ``revision``, or None when that tree has no such file."""
    listing = _run_git(repo_root, ("ls-tree", "--name-only", revision, "--", path))
    if not listing.strip():
        return None
    return _run_git(repo_root, ("show", f"{revision}:{path}"))


@pure
def split_against_creations(
    changed_files: Sequence[RepoRelativePath], creations: Sequence[CreationScope]
) -> tuple[tuple[RepoRelativePath, ...], tuple[RepoRelativePath, ...]]:
    """The changed files some creation's footprint accounts for, and those none does.

    A file is inside when a creation's footprint covers it and that creation does not
    exclude it. Otherwise a file the built-ins or any creation excludes is in neither list.
    """
    built_in_spec = pathspec.PathSpec.from_lines(_EXCLUDE_PATTERN_STYLE, BUILT_IN_EXCLUDES)
    exclude_specs = [
        (creation, pathspec.PathSpec.from_lines(_EXCLUDE_PATTERN_STYLE, creation.exclude))
        for creation in creations
    ]
    inside: list[RepoRelativePath] = []
    outside: list[RepoRelativePath] = []
    for changed_file in changed_files:
        if built_in_spec.match_file(changed_file):
            continue
        if any(
            is_inside_footprint(changed_file, creation) and not exclude_spec.match_file(changed_file)
            for creation, exclude_spec in exclude_specs
        ):
            inside.append(changed_file)
        elif not any(exclude_spec.match_file(changed_file) for _, exclude_spec in exclude_specs):
            outside.append(changed_file)
    return tuple(inside), tuple(outside)


@pure
def _summarize_diff(changed: ChangedFiles, creations: Sequence[CreationScope]) -> DiffSummary:
    inside_footprint, outside_footprint = split_against_creations(changed.files, creations)
    return DiffSummary(
        base=changed.base,
        ref=changed.ref,
        files=changed.files,
        inside_footprint=inside_footprint,
        outside_footprint=outside_footprint,
    )


def with_diff_against_base(
    scope: CreationScope, repo_root: Path, diff_base: str, diff_ref: str
) -> CreationScope:
    """The same scope with its diff filled in from the changed files between a base and a ref.

    A ref other than HEAD lets one tree answer for a range that does not end at it, such as
    what a merge commit's first parent changed since the fork point.
    """
    diff_summary = _summarize_diff(list_changed_files(repo_root, diff_base, diff_ref), (scope,))
    return scope.model_copy_update(to_update(scope.field_ref().diff, diff_summary))


@pure
def render_scope_file(scope: CreationScope) -> str:
    """The scope file's JSON text, indented and newline-terminated."""
    return f"{json.dumps(scope.model_dump(mode='json'), indent=2)}\n"


def compute_service_scope(repo_root: Path, package: str) -> CreationScope:
    """The footprint of the standalone service in ``system/services/<package>/``: its directory
    and the programs named after it, with no diff attached yet."""
    return _compute_unmanifested_scope(
        repo_root, CreationType.SERVICE, f"{_SERVICES_DIRECTORY}/{package}", SERVICE_CONVENTIONS
    )


def _compute_unmanifested_scope(
    repo_root: Path,
    creation_type: CreationType,
    directory: str,
    conventions: tuple[RepoRelativePath, ...],
) -> CreationScope:
    """Raises ScopeComputationError when ``directory`` is missing, for the reason
    ``compute_skill_scope`` gives."""
    repo_root = repo_root.resolve()
    if not (repo_root / directory).is_dir():
        raise ScopeComputationError(f"{directory!r} is not a directory under {repo_root}")
    package = Path(directory).name
    return CreationScope(
        creation=CreationIdentity(
            type=creation_type, name=NonEmptyStr(package), package=NonEmptyStr(package), manifest=None
        ),
        primary=(RepoRelativePath(f"{directory}/"),),
        wiring=find_named_wiring_sections(repo_root, program_name_for_package(package)),
        references=(),
        context=(),
        conventions=conventions,
        exclude=BUILT_IN_EXCLUDES,
        diff=None,
    )


class CreationSpecType(LowerCaseStrEnum):
    """What a lead names a creation as; a shared script or reference is scoped as a skill is."""

    APP = auto()
    SKILL = auto()
    SERVICE = auto()
    SHARED = auto()


class CreationSpec(FrozenModel):
    """One creation a pass covers, as a lead names it: ``<type>:<name>``."""

    type: CreationSpecType = Field(description="app, skill, service, or shared")
    name: NonEmptyStr = Field(
        description="An app's registered name or package directory; a skill's or service's directory "
        "name; a shared script's or reference's path under .agents/shared"
    )


@pure
def parse_creation_spec(spec: str) -> CreationSpec:
    """Read ``<type>:<name>``; raises ScopeComputationError on any other shape. A name is a
    directory or file name; only a shared creation's is a path, of such names."""
    type_text, separator, name = spec.partition(":")
    types = [member.value for member in CreationSpecType]
    segments = name.split("/")
    is_path_allowed = type_text == CreationSpecType.SHARED
    if (
        not separator
        or type_text not in types
        or (len(segments) > 1 and not is_path_allowed)
        or not all(_CREATION_NAME_SEGMENT.fullmatch(segment) for segment in segments)
    ):
        raise ScopeComputationError(f"{spec!r} is not <type>:<name> with a type of {', '.join(types)}")
    return CreationSpec(type=CreationSpecType(type_text), name=NonEmptyStr(name))


def compute_creation_scope(repo_root: Path, spec: CreationSpec) -> CreationScope:
    """The footprint of the creation ``spec`` names, with no diff attached yet.

    An app is found by its package directory, then by its registered name. An app directory
    with no manifest yet is scoped as a service is: its directory and the programs named
    after it.
    """
    repo_root = repo_root.resolve()
    match spec.type:
        case CreationSpecType.SKILL:
            return compute_skill_scope(repo_root, RepoRelativePath(f"{_SKILLS_DIRECTORY}/{spec.name}"))
        case CreationSpecType.SHARED:
            return compute_skill_scope(repo_root, RepoRelativePath(f"{_SHARED_DIRECTORY}/{spec.name}"))
        case CreationSpecType.SERVICE:
            return compute_service_scope(repo_root, spec.name)
        case CreationSpecType.APP:
            return _compute_named_app_scope(repo_root, spec.name)
        case _ as unreachable:
            assert_never(unreachable)


def _compute_named_app_scope(repo_root: Path, name: str) -> CreationScope:
    manifest_path = repo_root.joinpath(*APPS_DIRECTORY_PARTS, name, MANIFEST_FILENAME)
    if manifest_path.is_file():
        return compute_app_scope(repo_root, manifest_path, load_manifest(manifest_path, repo_root=repo_root))
    for loaded in load_app_manifests(repo_root):
        if loaded.manifest.name == name:
            return compute_app_scope(repo_root, loaded.manifest_path, loaded.manifest)
    return _compute_unmanifested_scope(
        repo_root, CreationType.APP, "/".join((*APPS_DIRECTORY_PARTS, name)), APP_CONVENTIONS
    )


class PassScope(FrozenModel):
    """Every creation a harden pass covers, and the pass's diff judged against all of them."""

    creations: tuple[CreationScope, ...] = Field(description="Each creation's footprint, without a diff")
    diff: DiffSummary | None = Field(
        description="The diff against a base, split by whether any creation's footprint accounts for each "
        "file, when the caller asks for one"
    )


def compute_pass_scope(
    repo_root: Path, specs: Sequence[CreationSpec], diff_base: str | None, diff_ref: str
) -> PassScope:
    """The scope of a pass over ``specs``, with its diff when ``diff_base`` is given."""
    if not specs:
        raise ScopeComputationError("a pass covers at least one creation")
    repeated = sorted({f"{spec.type}:{spec.name}" for spec in specs if specs.count(spec) > 1})
    if repeated:
        raise ScopeComputationError(f"a pass names {', '.join(repeated)} more than once")
    creations = tuple(compute_creation_scope(repo_root, spec) for spec in specs)
    if diff_base is None:
        return PassScope(creations=creations, diff=None)
    return PassScope(
        creations=creations,
        diff=_summarize_diff(list_changed_files(repo_root.resolve(), diff_base, diff_ref), creations),
    )


@pure
def render_pass_scope_file(scope: PassScope) -> str:
    """The pass scope file's JSON text, indented and newline-terminated."""
    return f"{json.dumps(scope.model_dump(mode='json'), indent=2)}\n"
