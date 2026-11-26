"""Publishing an app: the steps of a publish that need no judgement.

An app's published repository holds one commit per published version. Each is the app's directory at one
workspace commit, less the paths the manifest's ``[publish] exclude`` names, and it is pushed only after the
secret scan found nothing in exactly that tree. The steps are separate commands because a person decides between
them: what to publish before the tree is assembled, and whether to make it public before it is pushed.

- ``compare`` says what changed since the last publish, for the version to be chosen.
- ``set_version`` writes the version into the app, with the template version the workspace is on.
- ``assemble`` builds the tree, scans it, and checks that the app stands alone: it names no skill of this
  workspace that it does not carry, and a Python app locks with no workspace around it.
- ``push`` creates the repository on the first publish, and pushes the assembled tree as one commit.
- ``record`` writes what was published into the app's record.

What ``assemble`` built waits for ``push`` in a file under the installer's state directory, and so does what
``push`` pushed for ``record``: a step that was interrupted is picked up by running it again.
"""

import json
import re
import shutil
import tempfile
import tomllib
from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from enum import auto
from pathlib import Path
from typing import Final

import pathspec
import tomlkit
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from loguru import logger
from pydantic import Field
from pydantic import ValidationError
from tomlkit.exceptions import TOMLKitError

from app_install.derived import is_python_app
from app_install.errors import GitCommandError
from app_install.errors import LockCheckError
from app_install.errors import NotAssembledError
from app_install.errors import NotModifiedAppError
from app_install.errors import NotPushedError
from app_install.errors import OutsideSkillsError
from app_install.errors import PublishError
from app_install.errors import PushRefusedError
from app_install.errors import RepositoryUnreachableError
from app_install.errors import SecretsFoundError
from app_install.git import DEFAULT_BRANCH
from app_install.git import fetch_commit
from app_install.git import is_commit_present
from app_install.git import is_path_in_commit
from app_install.git import run_git
from app_install.git import workspace_template_version
from app_install.interfaces import WorkspaceRuntimeInterface
from app_install.paths import APP_SKILLS_DIRNAME
from app_install.paths import MANIFEST_FILENAME
from app_install.paths import PUBLISHED_REF_PREFIX
from app_install.paths import PYPROJECT_FILENAME
from app_install.paths import SKILL_FILENAME
from app_install.paths import SKILLS_DIRECTORY
from app_install.paths import STATE_DIRECTORY
from app_install.paths import app_directory
from app_install.paths import manifest_path
from app_manifest.errors import AppManifestError
from app_manifest.manifest import APP_VERSION_PATTERN
from app_manifest.manifest import AppManifest
from app_manifest.manifest import ManifestReadMode
from app_manifest.manifest import PublishRules
from app_manifest.manifest import check_declares_no_record_tables
from app_manifest.manifest import describe_validation_error
from app_manifest.manifest import load_manifest
from app_manifest.records import AppOrigin
from app_manifest.records import AppRecord
from app_manifest.records import app_directory_prefix
from app_manifest.records import load_record
from app_manifest.records import modifying_paths
from app_manifest.records import write_record
from github_sync.repositories import hint_for_push_failure

# A published tree can hold an app's whole directory, which the small in-memory /tmp of a workspace is not for.
_SCRATCH_PARENT: Final[Path] = Path("/var/tmp")
_SCRATCH_PREFIX: Final[str] = "app-install-publish-"
_PENDING_DIRNAME: Final[str] = "publish"
_TREE_DIRNAME: Final[str] = "tree"
_INDEX_FILENAME: Final[str] = "index"
# pathspec's pattern factory for gitignore syntax, which is what a publish exclude is written in.
_EXCLUDE_PATTERN_STYLE: Final[str] = "gitignore"
_VERSION_TAG_PREFIX: Final[str] = "v"
_DERIVED_FROM_TRAILER: Final[str] = "Derived-From"
_REPOSITORY_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")
_VERSION_CORE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-(.+))?$")
_NON_FAST_FORWARD_MARKERS: Final[tuple[str, ...]] = ("(non-fast-forward)", "(fetch first)")
_ALREADY_EXISTS_MARKER: Final[str] = "already exists"
# How many paths one git command is given, so that its arguments stay within what a command line can hold.
_PATHS_PER_COMMAND: Final[int] = 200
# A skill named by where it lives in a workspace, or by where it keeps its data there.
_SKILL_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:\.agents/skills|data/\.skills)/([a-z0-9]+(?:-[a-z0-9]+)*)"
)
# A larger file is data or a bundle, not where an app names something it runs or reads.
_LARGEST_FILE_READ_FOR_SKILL_PATHS_BYTES: Final[int] = 1_000_000


class ChangeKind(LowerCaseStrEnum):
    """What happened to a file of the app since it was last published."""

    ADDED = auto()
    MODIFIED = auto()
    DELETED = auto()


class ChangedPath(FrozenModel):
    """One file of the app that differs from the version last published."""

    path: str = Field(description="The file, relative to the app's directory")
    kind: ChangeKind = Field(description="Whether it is new, changed, or gone")


class PublishedTree(FrozenModel):
    """The tree a publish of one workspace commit would push."""

    source_commit: str = Field(description="The workspace commit the app's directory was taken at")
    tree: str = Field(description="The tree: the app's directory at that commit, less what it excludes")
    paths: tuple[str, ...] = Field(description="Every file in the tree, relative to the app's directory")
    excluded_paths: tuple[str, ...] = Field(description="The files [publish] exclude left out")


class OutsideSkillUse(FrozenModel):
    """One line of a tree to publish that names a skill of this workspace the app does not carry."""

    skill: str = Field(description="The skill's name")
    path: str = Field(description="The file that names it, relative to the app's directory")
    line: int = Field(description="The line of the file, counted from one")


class PendingPublish(FrozenModel):
    """A publish between its steps: what was assembled, and how much of it has reached the repository."""

    source_commit: str = Field(description="The workspace commit the tree was assembled from")
    tree: str = Field(description="The assembled tree, which the scan found nothing in")
    version: str = Field(description="The version the tree's manifest names")
    description: str = Field(default="", description="The app's one-line description, for a new repository")
    verified_template: str = Field(default="", description="The template version the tree's manifest names")
    commit: str = Field(
        default="", description="The commit made from the tree, which a push that is run again pushes again"
    )
    is_commit_pushed: bool = Field(default=False, description="Whether the commit reached the repository's main")
    is_tag_pushed: bool = Field(default=False, description="Whether the version's tag reached the repository")


class CompareOutcome(FrozenModel):
    """What changed in an app since it was last published."""

    record: AppRecord = Field(description="The app's record")
    published: PublishedTree = Field(description="The tree a publish of the commit would push")
    version: str = Field(description="The version the app's manifest names at the commit, empty when none")
    changed_paths: tuple[ChangedPath, ...] = Field(
        description="The files that differ from the version last published; empty for an app never published"
    )
    outside_skill_uses: tuple[OutsideSkillUse, ...] = Field(
        description="Each line that names a skill of this workspace the app does not carry, which stops a publish"
    )
    is_outside_skills_checked: bool = Field(
        description="Whether the tree was read for such skills, which takes a template release in the history"
    )


class VersionOutcome(FrozenModel):
    """What setting an app's version wrote."""

    record: AppRecord = Field(description="The app's record")
    version: str = Field(description="The version the app now names")
    previous_version: str = Field(description="The version its manifest named before, empty when none")
    verified_template: str = Field(description="The template version written beside it, empty when none was")
    changed_paths: tuple[str, ...] = Field(description="Every file written, repo-relative")
    problems: tuple[str, ...] = Field(default=(), description="What could not be done; the version is written")


class AssembleOutcome(FrozenModel):
    """A tree that was assembled and found fit to publish."""

    record: AppRecord = Field(description="The app's record")
    published: PublishedTree = Field(description="The tree that waits to be pushed")
    manifest: AppManifest = Field(description="The manifest in the tree, for what it asks an adopter to do")
    is_lock_checked: bool = Field(description="Whether the tree holds a Python project, whose lock was checked")
    is_outside_skills_checked: bool = Field(
        description="Whether the tree was read for skills it names and does not carry, which takes a template "
        "release in the history"
    )


class PushOutcome(FrozenModel):
    """What a push did."""

    record: AppRecord = Field(
        description="The app's record as it is now: it names the published repository once that exists"
    )
    repo_url: str = Field(description="The published repository")
    commit: str = Field(description="The commit on the repository's main branch")
    parent: str = Field(description="The published commit it follows, empty for the first")
    version: str = Field(description="The version published")
    tag: str = Field(description="The version's tag")
    is_repository_created: bool = Field(description="Whether this run created the repository")
    is_tag_pushed: bool = Field(description="Whether the tag reached the repository")
    derived_from: str = Field(description="What the commit says the app was derived from, empty when nothing")
    problems: tuple[str, ...] = Field(default=(), description="What this run tried and could not do")


class RecordOutcome(FrozenModel):
    """What recording a publish wrote."""

    record: AppRecord = Field(description="The app's record as it is now")
    problems: tuple[str, ...] = Field(default=(), description="What this run tried and could not do")


@contextmanager
def _scratch_directory() -> Generator[Path, None, None]:
    parent = _SCRATCH_PARENT if _SCRATCH_PARENT.is_dir() else None
    directory = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=parent))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def pending_publish_path(repo_root: Path, name: str) -> Path:
    """Where a publish of the app ``name`` waits between its steps."""
    return repo_root / STATE_DIRECTORY / _PENDING_DIRNAME / f"{name}.json"


def _load_pending(repo_root: Path, name: str) -> PendingPublish:
    path = pending_publish_path(repo_root, name)
    try:
        return PendingPublish.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError as e:
        raise NotAssembledError(
            f"no assembled tree of {name} is waiting: run `app-install publish assemble {name}` first"
        ) from e
    except (OSError, ValueError) as e:
        raise NotAssembledError(
            f"what was assembled for {name} cannot be read ({e}): run `app-install publish assemble {name}` again"
        ) from e


def _write_pending(repo_root: Path, name: str, pending: PendingPublish) -> None:
    path = pending_publish_path(repo_root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pending.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _pre_release_key(pre_release: str) -> tuple[tuple[int, int, str], ...]:
    """A pre-release's place among the pre-releases of one version: its dot-separated identifiers are compared
    in turn, a number by its value and before anything that is not one, and ``rc`` comes before ``rc.1``."""
    return tuple(
        (0, int(identifier), "") if identifier.isdecimal() else (1, 0, identifier)
        for identifier in pre_release.split(".")
    )


def _version_key(version: str) -> tuple[int, int, int, int, tuple[tuple[int, int, str], ...]]:
    """A version's place in semantic order: a pre-release comes before the release it leads to."""
    match = _VERSION_CORE_PATTERN.fullmatch(version)
    if match is None:
        raise PublishError(f"{version!r} is not a semantic version such as 1.2.3")
    pre_release = match.group(4) or ""
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        0 if pre_release else 1,
        _pre_release_key(pre_release),
    )


def _check_follows_published(version: str, record: AppRecord) -> None:
    """Raise PublishError unless ``version`` is newer than the one last published from this workspace."""
    if not APP_VERSION_PATTERN.fullmatch(version):
        raise PublishError(f"{version!r} is not a semantic version such as 1.2.3")
    if record.published_version and _version_key(version) <= _version_key(record.published_version):
        raise PublishError(
            f"{record.name} was last published as {record.published_version}, so the next version must be newer "
            f"than that, and {version} is not"
        )


def _read_publish_excludes(repo_root: Path, app_tree: str, record: AppRecord) -> tuple[str, ...]:
    """The ``[publish] exclude`` globs of the manifest the tree holds."""
    try:
        text = run_git(repo_root, ["show", f"{app_tree}:{MANIFEST_FILENAME}"])
    except GitCommandError as e:
        raise PublishError(f"{app_directory_prefix(record)} holds no {MANIFEST_FILENAME} at that commit") from e
    try:
        table = tomllib.loads(text).get("publish", {})
    except tomllib.TOMLDecodeError as e:
        raise PublishError(f"the {MANIFEST_FILENAME} of {record.name} is not valid TOML: {e}") from e
    try:
        return tuple(PublishRules.model_validate(table).exclude)
    except ValidationError as e:
        raise PublishError(f"the [publish] table of {record.name} is invalid: {describe_validation_error(e)}") from e
    except AppManifestError as e:
        raise PublishError(f"the [publish] table of {record.name} is invalid: {e}") from e


def _build_published_tree(repo_root: Path, record: AppRecord, revision: str, index_file: Path) -> PublishedTree:
    """Build, in an index of its own, the tree a publish of ``revision`` would push: the app's directory at that
    commit, less what its manifest excludes. Nothing in the checkout or its index is touched."""
    prefix = app_directory_prefix(record)
    try:
        source_commit = run_git(repo_root, ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"])
        app_tree = run_git(repo_root, ["rev-parse", "--verify", "--quiet", f"{source_commit}:{prefix}"])
    except GitCommandError as e:
        raise PublishError(
            f"{revision} is not a commit that holds {prefix}: commit the app, then publish that commit"
        ) from e
    excludes = _read_publish_excludes(repo_root, app_tree, record)
    index = {"GIT_INDEX_FILE": str(index_file)}
    run_git(repo_root, ["read-tree", app_tree], extra_environment=index)
    listed = run_git(repo_root, ["ls-files", "--cached", "-z"], extra_environment=index)
    all_paths = [path for path in listed.split("\0") if path]
    spec = pathspec.PathSpec.from_lines(_EXCLUDE_PATTERN_STYLE, excludes)
    excluded = [path for path in all_paths if spec.match_file(path)]
    for start in range(0, len(excluded), _PATHS_PER_COMMAND):
        batch = excluded[start : start + _PATHS_PER_COMMAND]
        run_git(repo_root, ["update-index", "--force-remove", "--", *batch], extra_environment=index)
    excluded_set = set(excluded)
    return PublishedTree(
        source_commit=source_commit,
        tree=run_git(repo_root, ["write-tree"], extra_environment=index),
        paths=tuple(path for path in all_paths if path not in excluded_set),
        excluded_paths=tuple(excluded),
    )


def _check_out_tree(repo_root: Path, index_file: Path, tree_directory: Path) -> None:
    """Write the files of the tree ``index_file`` holds into the new directory ``tree_directory``."""
    tree_directory.mkdir()
    run_git(
        repo_root,
        ["checkout-index", "--all", f"--prefix={tree_directory}/"],
        extra_environment={"GIT_INDEX_FILE": str(index_file)},
    )


def _find_outside_skill_uses(
    repo_root: Path, published: PublishedTree, template_version: str, tree_directory: Path
) -> tuple[OutsideSkillUse, ...]:
    """Each line of the checked-out tree that names, by its path under ``.agents/skills/`` or ``data/.skills/``,
    a skill that would be missing where the app is installed: one the workspace holds at the published commit,
    the app does not carry in its own ``skills/``, and the template release ``template_version`` does not ship.
    """
    is_outside_by_skill: dict[str, bool] = {}
    uses: list[OutsideSkillUse] = []
    for path in published.paths:
        file = tree_directory / path
        if file.is_symlink() or file.stat().st_size > _LARGEST_FILE_READ_FOR_SKILL_PATHS_BYTES:
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            logger.debug("Reading no skill paths from {}, which is not text: {}", path, e)
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for skill in dict.fromkeys(match.group(1) for match in _SKILL_PATH_PATTERN.finditer(line)):
                if skill not in is_outside_by_skill:
                    skill_path = (SKILLS_DIRECTORY / skill).as_posix()
                    is_outside_by_skill[skill] = (
                        not (tree_directory / APP_SKILLS_DIRNAME / skill / SKILL_FILENAME).is_file()
                        and is_path_in_commit(repo_root, published.source_commit, skill_path)
                        and not is_path_in_commit(repo_root, template_version, skill_path)
                    )
                if is_outside_by_skill[skill]:
                    uses.append(OutsideSkillUse(skill=skill, path=path, line=line_number))
    return tuple(uses)


def _ensure_published_commit_present(repo_root: Path, record: AppRecord) -> None:
    """Have the commit last published in the repository, fetching it from the published repository when a
    restored workspace has lost it. Raises PublishError when it cannot be had."""
    if is_commit_present(repo_root, record.published_commit):
        return
    try:
        fetch_commit(repo_root, record.published_repo_url, DEFAULT_BRANCH)
    except (GitCommandError, RepositoryUnreachableError) as e:
        raise PublishError(
            f"the commit last published for {record.name}, {record.published_commit}, is not in the workspace "
            f"repository and could not be fetched from {record.published_repo_url}: {e}"
        ) from e
    if not is_commit_present(repo_root, record.published_commit):
        raise PublishError(
            f"{record.published_repo_url} does not hold {record.published_commit}, the commit the app's record "
            f"says was last published for {record.name}"
        )


def _changed_paths(repo_root: Path, record: AppRecord, tree: str) -> tuple[ChangedPath, ...]:
    """The files in which ``tree`` differs from the commit last published; none for an app never published."""
    if not record.published_commit:
        return ()
    _ensure_published_commit_present(repo_root, record)
    listing = run_git(repo_root, ["diff", "--name-status", "--no-renames", "-z", record.published_commit, tree])
    fields = [field for field in listing.split("\0") if field]
    kind_by_status = {"A": ChangeKind.ADDED, "D": ChangeKind.DELETED}
    return tuple(
        ChangedPath(path=path, kind=kind_by_status.get(status[:1], ChangeKind.MODIFIED))
        for status, path in zip(fields[0::2], fields[1::2], strict=True)
    )


def _manifest_version_at(repo_root: Path, tree: str) -> str:
    try:
        version = tomllib.loads(run_git(repo_root, ["show", f"{tree}:{MANIFEST_FILENAME}"])).get("version")
    except (GitCommandError, tomllib.TOMLDecodeError) as e:
        logger.debug("No readable {} in the tree {}: {}", MANIFEST_FILENAME, tree, e)
        return ""
    return version if isinstance(version, str) else ""


def compare_with_published(repo_root: Path, name: str, revision: str) -> CompareOutcome:
    """What a publish of the app ``name`` at ``revision`` would push, and how that differs from the version last
    published from this workspace: what the next version's number is chosen from.

    Raises AppRecordNotFoundError for an app with no record, and PublishError when the commit does not hold the
    app or the commit last published cannot be had.
    """
    record = load_record(repo_root, name)
    template_version = workspace_template_version(repo_root)
    with _scratch_directory() as scratch:
        index_file = scratch / _INDEX_FILENAME
        published = _build_published_tree(repo_root, record, revision, index_file)
        outside_skill_uses: tuple[OutsideSkillUse, ...] = ()
        if template_version:
            tree_directory = scratch / _TREE_DIRNAME
            _check_out_tree(repo_root, index_file, tree_directory)
            outside_skill_uses = _find_outside_skill_uses(repo_root, published, template_version, tree_directory)
    return CompareOutcome(
        record=record,
        published=published,
        version=_manifest_version_at(repo_root, published.tree),
        changed_paths=_changed_paths(repo_root, record, published.tree),
        outside_skill_uses=outside_skill_uses,
        is_outside_skills_checked=bool(template_version),
    )


def _set_toml_keys(path: Path, table: str | None, value_by_key: dict[str, str]) -> None:
    """Set string keys of a TOML file, in ``table`` or at the top level, keeping its comments and order."""
    try:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, TOMLKitError) as e:
        raise PublishError(f"{path} cannot be rewritten: {e}") from e
    container = document if table is None else document.get(table)
    if container is None:
        raise PublishError(f"{path} has no [{table}] table to write the version into")
    for key, value in value_by_key.items():
        container[key] = value
    path.write_text(document.as_string(), encoding="utf-8")


def set_version(repo_root: Path, name: str, version: str, runtime: WorkspaceRuntimeInterface) -> VersionOutcome:
    """Write ``version`` into the app ``name``: into its manifest, with the template version the workspace is on
    as ``verified_template``, and, for a Python app, into its project, whose lock is then made again. Nothing is
    committed: the publish's worker commits its own work.

    Raises AppRecordNotFoundError for an app with no record, and PublishError for a version that is not a
    semantic version or not newer than the one last published, and when the manifest it leaves cannot be read,
    in which case every file is as it was.
    """
    record = load_record(repo_root, name)
    _check_follows_published(version, record)
    manifest_file = manifest_path(repo_root, record.package)
    if not manifest_file.is_file():
        raise PublishError(f"no app at {app_directory_prefix(record)}")
    project_file = app_directory(repo_root, record.package) / PYPROJECT_FILENAME
    is_python = is_python_app(repo_root, record.package)
    original_text_by_file = {manifest_file: manifest_file.read_text(encoding="utf-8")}
    if is_python:
        original_text_by_file[project_file] = project_file.read_text(encoding="utf-8")
    previous_version = _load_manifest_for_publish(manifest_file, record).version or ""
    template_version = workspace_template_version(repo_root)
    keys = {"version": version, **({"verified_template": template_version} if template_version else {})}
    try:
        _set_toml_keys(manifest_file, None, keys)
        if is_python:
            _set_toml_keys(project_file, "project", {"version": version})
        _load_manifest_for_publish(manifest_file, record)
    except PublishError:
        for path, text in original_text_by_file.items():
            path.write_text(text, encoding="utf-8")
        raise
    changed = [path.relative_to(repo_root).as_posix() for path in original_text_by_file]
    problems: list[str] = []
    if not template_version:
        problems.append(
            "the workspace's history holds no template release tag, so verified_template was left as it was"
        )
    if is_python:
        relock_failure = runtime.relock_app(repo_root, record.package)
        if relock_failure is None:
            changed.append(f"{app_directory_prefix(record)}/uv.lock")
        else:
            problems.append(f"the app's lock was not made again, and it names the app's own version: {relock_failure}")
    return VersionOutcome(
        record=record,
        version=version,
        previous_version=previous_version,
        verified_template=template_version,
        changed_paths=tuple(changed),
        problems=tuple(problems),
    )


def _load_manifest_for_publish(path: Path, record: AppRecord) -> AppManifest:
    """The manifest at ``path``, held to the authoring check when the workspace built the app. An app that was
    installed and changed here is read tolerantly: its first publisher may run a newer template."""
    mode = ManifestReadMode.STRICT if record.origin is AppOrigin.LOCAL else ManifestReadMode.TOLERANT
    try:
        manifest = load_manifest(path, mode=mode)
        if mode is ManifestReadMode.STRICT:
            # The published tree sits outside any workspace, where the load does not know the app has a record.
            check_declares_no_record_tables(path, manifest)
    except AppManifestError as e:
        raise PublishError(f"the {MANIFEST_FILENAME} of {record.name} does not pass the authoring check: {e}") from e
    if manifest.name != record.name:
        raise PublishError(
            f"the {MANIFEST_FILENAME} of {record.name} names the app {str(manifest.name)!r}, not {str(record.name)!r}"
        )
    return manifest


def _check_is_this_workspaces_to_publish(repo_root: Path, record: AppRecord, published: PublishedTree) -> None:
    """Raise NotModifiedAppError for an installed app the workspace has not changed: it is its publisher's."""
    if record.origin is not AppOrigin.INSTALLED:
        return
    app_tree = run_git(repo_root, ["rev-parse", f"{published.source_commit}:{app_directory_prefix(record)}"])
    try:
        differing = run_git(repo_root, ["diff", "--name-only", "--no-renames", record.upstream_tree, app_tree])
    except GitCommandError as e:
        # Without the installed tree the app cannot be shown to be unchanged, as when its state is listed.
        logger.warning(
            "Could not compare {} with the tree it was installed from, so it is taken as modified: {}", record.name, e
        )
        return
    if not modifying_paths(differing.splitlines()):
        raise NotModifiedAppError(
            f"{record.name} was installed from {record.upstream_url} and is unchanged, so there is nothing of "
            "this workspace's to publish"
        )


def _check_nothing_pushed_is_unrecorded(repo_root: Path, name: str) -> None:
    """Raise PublishError when a version that was pushed still waits to be recorded: assembling the next one
    would forget which commit that was, and the next push could not follow it."""
    try:
        pending = _load_pending(repo_root, name)
    except NotAssembledError as e:
        logger.debug("Nothing of {} is waiting between two steps of a publish: {}", name, e)
        return
    if pending.is_commit_pushed:
        raise PublishError(
            f"{name} {_VERSION_TAG_PREFIX}{pending.version} was pushed and is not recorded yet: run "
            f"`app-install publish record {name}` before assembling the next version"
        )


def assemble_app(repo_root: Path, name: str, revision: str, runtime: WorkspaceRuntimeInterface) -> AssembleOutcome:
    """Build the tree a publish of the app ``name`` at ``revision`` would push, in a directory outside the
    workspace, and find it fit to publish: its manifest passes the authoring check and names a version newer
    than the last one published, the secret scan finds nothing in it, it names no skill of this workspace that
    it does not carry, and, for a Python app, its lock is what its project calls for with no workspace around
    it. The tree then waits for ``push_app``. Nothing is pushed.

    Raises AppRecordNotFoundError for an app with no record, NotModifiedAppError for an installed app nobody
    has changed, SecretsFoundError with each finding, OutsideSkillsError with each line that names such a
    skill, LockCheckError, and PublishError for everything else,
    which includes a version that was pushed and still waits to be recorded. A tree that was waiting from an
    earlier assembly is gone when it raises for any other reason.
    """
    record = load_record(repo_root, name)
    _check_nothing_pushed_is_unrecorded(repo_root, name)
    pending_publish_path(repo_root, name).unlink(missing_ok=True)
    with _scratch_directory() as scratch:
        index_file = scratch / _INDEX_FILENAME
        published = _build_published_tree(repo_root, record, revision, index_file)
        _check_is_this_workspaces_to_publish(repo_root, record, published)
        tree_directory = scratch / _TREE_DIRNAME
        _check_out_tree(repo_root, index_file, tree_directory)
        if not (tree_directory / MANIFEST_FILENAME).is_file():
            raise PublishError(
                f"[publish] exclude leaves {MANIFEST_FILENAME} out of the published tree, and a repository "
                "without one is not an app"
            )
        manifest = _load_manifest_for_publish(tree_directory / MANIFEST_FILENAME, record)
        if manifest.version is None:
            raise PublishError(
                f"the {MANIFEST_FILENAME} of {name} names no version: set one with "
                f"`app-install publish set-version {name} <version>` and commit it"
            )
        _check_follows_published(manifest.version, record)
        scan = runtime.scan_secrets(tree_directory)
        if scan.findings:
            raise SecretsFoundError(scan.findings)
        if scan.failure:
            raise PublishError(f"the secret scan did not run to the end, so nothing can be published: {scan.failure}")
        template_version = workspace_template_version(repo_root)
        if template_version:
            outside_skill_uses = _find_outside_skill_uses(repo_root, published, template_version, tree_directory)
            if outside_skill_uses:
                raise OutsideSkillsError(
                    tuple(
                        f"{use.path}:{use.line} names the skill {use.skill}, which this workspace holds and the "
                        "app does not carry"
                        for use in outside_skill_uses
                    )
                )
        is_lock_checked = (tree_directory / PYPROJECT_FILENAME).is_file()
        if is_lock_checked:
            lock_failure = runtime.check_lock(tree_directory)
            if lock_failure is not None:
                raise LockCheckError(
                    f"the lock of {name} is not what its project calls for when the app stands alone, outside "
                    f"the workspace: {lock_failure}"
                )
    _write_pending(
        repo_root,
        name,
        PendingPublish(
            source_commit=published.source_commit,
            tree=published.tree,
            version=manifest.version,
            description=str(manifest.description or manifest.display_name),
            verified_template=manifest.verified_template or "",
        ),
    )
    return AssembleOutcome(
        record=record,
        published=published,
        manifest=manifest,
        is_lock_checked=is_lock_checked,
        is_outside_skills_checked=bool(template_version),
    )


def _derived_from(record: AppRecord) -> str:
    """What the first publish of an app that was installed and changed here says it came from."""
    if record.origin is not AppOrigin.INSTALLED or record.published_commit or not record.upstream_url:
        return ""
    return f"{record.upstream_url}@{record.upstream_commit}"


def _push(repo_root: Path, repo_url: str, refspec: str, earlier_problems: Sequence[str] = ()) -> None:
    """Push one refspec to the published repository through the latchkey gateway, never forced. Raises
    PushRefusedError when the repository holds commits the push does not continue, and PublishError otherwise.
    Either one also says ``earlier_problems``: what the run could not do before the push, which a run that
    stops here would otherwise never report."""
    earlier = f"; and before that: {'; '.join(earlier_problems)}" if earlier_problems else ""
    try:
        run_git(repo_root, ["push", "--quiet", repo_url, refspec], is_gateway_used=True)
    except GitCommandError as e:
        if any(marker in str(e) for marker in _NON_FAST_FORWARD_MARKERS):
            raise PushRefusedError(
                f"{repo_url} holds commits this workspace's publish does not continue, so nothing was pushed "
                f"and nothing is ever forced: {e}{earlier}"
            ) from e
        hint = hint_for_push_failure(str(e))
        raise PublishError(f"the push to {repo_url} failed: {e}" + (f" -- {hint}" if hint else "") + earlier) from e


def _commit_assembled_tree(repo_root: Path, record: AppRecord, pending: PendingPublish, derived_from: str) -> str:
    """Make the commit a publish pushes: the assembled tree, on the commit last published when there is one,
    with a message that names the version and what the app was derived from. Raises NotAssembledError when the
    repository no longer holds the tree."""
    name = str(record.name)
    try:
        run_git(repo_root, ["cat-file", "-e", f"{pending.tree}^{{tree}}"])
    except GitCommandError as e:
        raise NotAssembledError(
            f"the tree assembled for {name} is no longer in the workspace repository: run "
            f"`app-install publish assemble {name}` again"
        ) from e
    parent_arguments: list[str] = []
    if record.published_commit:
        _ensure_published_commit_present(repo_root, record)
        parent_arguments = ["-p", record.published_commit]
    message = f"{name} {_VERSION_TAG_PREFIX}{pending.version}"
    if derived_from:
        message = f"{message}\n\n{_DERIVED_FROM_TRAILER}: {derived_from}"
    return run_git(repo_root, ["commit-tree", pending.tree, *parent_arguments, "-m", message])


def push_app(
    repo_root: Path, name: str, repository_name: str | None, runtime: WorkspaceRuntimeInterface
) -> PushOutcome:
    """Push the tree ``assemble_app`` left waiting as the next version of the app ``name``.

    On the first publish the public repository ``repository_name`` (the app's name when None) is created and
    closed to outside contributions, and its URL is written to the app's record and committed, so that it is
    never created a second time. The tree becomes one commit, whose parent is the commit last published and
    whose message names the version and, for the first publish of an app that was installed and changed here,
    what it was derived from. The commit goes to the repository's ``main`` as a fast-forward and the tag
    ``v<version>`` after it, both through the latchkey gateway and neither forced.

    A run that was interrupted is finished by running it again: the commit is made once and kept, so a second
    run pushes the same one, which changes nothing in a repository that already has it. Raises
    AppRecordNotFoundError for an app with no record, NotAssembledError when no tree is waiting,
    PublishedNameTakenError when the account already has a repository of that name, PushRefusedError when the
    repository holds commits this publish does not continue, and PublishError otherwise. A tag that does not
    reach the repository is a problem in the outcome: the version is published all the same. What creating the
    repository could not do is a problem in the outcome too, and is said by the error when the push that
    follows it fails, since no later run creates the repository again.
    """
    record = load_record(repo_root, name)
    pending = _load_pending(repo_root, name)
    problems: list[str] = []
    is_repository_created = False
    repo_url = record.published_repo_url
    if not repo_url:
        chosen_name = repository_name or name
        if not _REPOSITORY_NAME_PATTERN.fullmatch(chosen_name):
            raise PublishError(
                f"{chosen_name!r} is not a repository name: letters, digits, '.', '_' and '-', not starting with '-'"
            )
        created = runtime.create_published_repository(chosen_name, pending.description)
        is_repository_created = True
        repo_url = created.url
        record = record.model_copy_update(to_update(record.field_ref().published_repo_url, repo_url))
        record_file = write_record(repo_root, record)
        commit_failure = runtime.commit_paths(
            [record_file.relative_to(repo_root).as_posix()], f"app-install: record the published repository of {name}"
        )
        if commit_failure is not None:
            problems.append(f"the repository's URL is in the app's record but was not committed: {commit_failure}")
        if created.lockdown_problem:
            problems.append(
                f"closing {repo_url} to outside contributions (issues, wiki, projects, discussions, outside "
                f"pull requests) did not take: {created.lockdown_problem}"
            )
        if created.topic_problem:
            problems.append(f"{repo_url} was not given the topic published apps are found by: {created.topic_problem}")

    derived_from = _derived_from(record)
    if not pending.is_commit_pushed:
        if not pending.commit:
            pending = pending.model_copy_update(
                to_update(pending.field_ref().commit, _commit_assembled_tree(repo_root, record, pending, derived_from))
            )
            _write_pending(repo_root, name, pending)
            # Keeps the published commit's objects in the workspace repository for the next publish to follow.
            run_git(repo_root, ["update-ref", f"{PUBLISHED_REF_PREFIX}{name}", pending.commit])
        # The next run finds the repository made and has nothing to say of how that went.
        _push(repo_root, repo_url, f"{pending.commit}:refs/heads/{DEFAULT_BRANCH}", problems)
        pending = pending.model_copy_update(to_update(pending.field_ref().is_commit_pushed, True))
        _write_pending(repo_root, name, pending)

    tag = f"{_VERSION_TAG_PREFIX}{pending.version}"
    if not pending.is_tag_pushed:
        try:
            _push(repo_root, repo_url, f"{pending.commit}:refs/tags/{tag}")
        except PublishError as e:
            if _ALREADY_EXISTS_MARKER in str(e):
                problems.append(f"{repo_url} already has a tag {tag}, which is never moved: {e}")
            else:
                problems.append(f"{pending.commit} is published, but its tag {tag} was not pushed: {e}")
        else:
            pending = pending.model_copy_update(to_update(pending.field_ref().is_tag_pushed, True))
            _write_pending(repo_root, name, pending)
    return PushOutcome(
        record=record,
        repo_url=repo_url,
        commit=pending.commit,
        parent=record.published_commit,
        version=pending.version,
        tag=tag,
        is_repository_created=is_repository_created,
        is_tag_pushed=pending.is_tag_pushed,
        derived_from=derived_from,
        problems=tuple(problems),
    )


def record_publish(repo_root: Path, name: str, runtime: WorkspaceRuntimeInterface) -> RecordOutcome:
    """Write what ``push_app`` published into the record of the app ``name`` (the repository, the commit, the
    version, and the template version the published manifest names) and commit that one file. It reads what was
    pushed from what the push left waiting, so a publish whose record was not written is finished by running
    this alone.

    Raises AppRecordNotFoundError for an app with no record, NotAssembledError when no publish is waiting, and
    NotPushedError when the waiting tree was never pushed. A record that is written but not committed is a
    problem in the outcome.
    """
    record = load_record(repo_root, name)
    pending = _load_pending(repo_root, name)
    if not pending.is_commit_pushed:
        raise NotPushedError(
            f"the tree assembled for {name} has not been pushed, so there is nothing to record: run "
            f"`app-install publish push {name}` first"
        )
    updated = record.model_copy_update(
        to_update(record.field_ref().published_commit, pending.commit),
        to_update(record.field_ref().published_version, pending.version),
        to_update(record.field_ref().verified_template, pending.verified_template or record.verified_template),
    )
    record_file = write_record(repo_root, updated)
    problems: list[str] = []
    commit_failure = runtime.commit_paths(
        [record_file.relative_to(repo_root).as_posix()],
        f"app-install: record that {name} {_VERSION_TAG_PREFIX}{pending.version} is published",
    )
    if commit_failure is None:
        pending_publish_path(repo_root, name).unlink(missing_ok=True)
    else:
        problems.append(
            f"the published version is in the app's record but was not committed, so run this again: {commit_failure}"
        )
    try:
        run_git(repo_root, ["merge-base", "--is-ancestor", pending.source_commit, "HEAD"])
    except GitCommandError as e:
        logger.debug("{} is not in the history of the checkout at {}: {}", pending.source_commit, repo_root, e)
        problems.append(
            f"the commit the published version was made from, {pending.source_commit}, is not in this checkout's "
            "history: merge the branch that holds it, so the workspace's copy of the app is what was published"
        )
    return RecordOutcome(record=updated, problems=tuple(problems))
