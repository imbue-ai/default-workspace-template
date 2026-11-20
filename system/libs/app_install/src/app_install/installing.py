"""Installing an app from someone else's repository.

The app enters the workspace repository with ``git subtree add --squash``, which keeps the
publisher's history out of it and leaves the join marker a later update or a split of the
app's own changes starts from. Everything is done in a worktree of the installer's own, since
``git subtree`` needs a clean tree and the primary checkout is the user's, and the result
reaches the primary checkout as one landed commit. Nothing changes there until then.
"""

import shutil
import tempfile
import tomllib
from collections.abc import Callable
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from loguru import logger
from pydantic import Field

from app_install.collisions import find_collisions
from app_install.collisions import workspace_app_names
from app_install.derived import derive_files
from app_install.derived import program_names
from app_install.derived import write_derived_files
from app_install.errors import CollisionError
from app_install.errors import GitCommandError
from app_install.errors import NotAnAppError
from app_install.git import fetch_commit
from app_install.git import land_commit
from app_install.git import resolve_ref
from app_install.git import run_git
from app_install.interfaces import WorkspaceRuntimeInterface
from app_install.paths import APPS_DIRECTORY
from app_install.paths import MANIFEST_FILENAME
from app_install.paths import SECRETS_DIRECTORY
from app_install.paths import UPSTREAM_REF_PREFIX
from app_install.paths import app_directory
from app_install.paths import manifest_path
from app_install.ports import allocate_port
from app_manifest.errors import AppManifestError
from app_manifest.errors import InvalidManifestValueError
from app_manifest.manifest import AppManifest
from app_manifest.manifest import ManifestReadMode
from app_manifest.manifest import load_manifest
from app_manifest.primitives import AppName
from app_manifest.records import AppOrigin
from app_manifest.records import AppRecord
from app_manifest.records import load_all_records
from app_manifest.records import record_path
from app_manifest.records import write_record

# A worktree can hold an app's whole tree, which the small in-memory /tmp of a workspace is not for.
_SCRATCH_PARENT: Final[Path] = Path("/var/tmp")
_SCRATCH_PREFIX: Final[str] = "app-install-"


class InstallOutcome(FrozenModel):
    """What installing an app did, and what the app still needs from the user."""

    record: AppRecord = Field(description="The record written for the app")
    ref: str = Field(description="The tag or branch of the repository that was installed")
    programs: tuple[str, ...] = Field(description="The supervisord programs the app runs")
    is_answering: bool = Field(description="Whether the app's port answered after its programs were started")
    missing_secret_files: tuple[str, ...] = Field(
        description="The secret files the app runs under that do not exist yet; its programs wait for them"
    )
    missing_dependencies: tuple[str, ...] = Field(description="The apps this app expects that are not here")
    unresolved_environment: tuple[str, ...] = Field(
        description="The system packages and tools the app declares that could not be installed"
    )
    manifest: AppManifest = Field(description="The installed app's manifest, for what it asks an adopter to do")
    problems: tuple[str, ...] = Field(
        description="What could not be done outside the tree (programs not started); the app is installed all the same"
    )


@contextmanager
def scratch_worktree(repo_root: Path) -> Generator[Path, None, None]:
    """A detached worktree of the repository at its current commit, removed when the block ends."""
    parent = _SCRATCH_PARENT if _SCRATCH_PARENT.is_dir() else None
    directory = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=parent))
    worktree = directory / "worktree"
    try:
        run_git(repo_root, ["worktree", "add", "--detach", str(worktree), "HEAD"])
    except GitCommandError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    try:
        yield worktree
    finally:
        try:
            run_git(repo_root, ["worktree", "remove", "--force", str(worktree)])
        except GitCommandError as e:
            logger.warning("Could not remove the installer's worktree {}: {}", worktree, e)
        shutil.rmtree(directory, ignore_errors=True)


def _read_published_name(repo_root: Path, commit: str, url: str) -> AppName:
    """The name of the app the commit holds; raises NotAnAppError when it holds none."""
    try:
        text = run_git(repo_root, ["show", f"{commit}:{MANIFEST_FILENAME}"])
    except GitCommandError as e:
        raise NotAnAppError(f"{url} has no {MANIFEST_FILENAME} at its root, so it is not an app") from e
    try:
        name = tomllib.loads(text).get("name")
    except tomllib.TOMLDecodeError as e:
        raise NotAnAppError(f"the {MANIFEST_FILENAME} of {url} is not valid TOML: {e}") from e
    if not isinstance(name, str):
        raise NotAnAppError(f"the {MANIFEST_FILENAME} of {url} names no app")
    try:
        return AppName(name)
    except InvalidManifestValueError as e:
        raise NotAnAppError(f"the {MANIFEST_FILENAME} of {url} names an app that cannot be registered: {e}") from e


def install_app(
    repo_root: Path,
    url: str,
    requested_ref: str | None,
    runtime: WorkspaceRuntimeInterface,
    is_bound: Callable[[int], bool],
) -> InstallOutcome:
    """Install the app published at ``url`` into the workspace, at ``requested_ref`` or its latest version.

    Raises RepositoryUnreachableError when the repository cannot be read, NotAnAppError when it holds no app,
    CollisionError when the app would take a name something in the workspace holds, and GitCommandError when the
    app cannot be added or landed. The primary checkout is unchanged when it raises.
    """
    ref = resolve_ref(repo_root, url, requested_ref)
    commit = fetch_commit(repo_root, url, ref)
    name = _read_published_name(repo_root, commit, url)
    package = name.replace("-", "_")
    relative_directory = (APPS_DIRECTORY / package).as_posix()
    if app_directory(repo_root, package).exists() or record_path(repo_root, name).exists():
        raise CollisionError((f"the app {str(name)!r} is already in this workspace at {relative_directory}",))

    with scratch_worktree(repo_root) as worktree:
        run_git(
            worktree,
            [
                "subtree",
                "add",
                f"--prefix={relative_directory}",
                commit,
                "--squash",
                "-m",
                f"app-install: install {name} from {url}",
            ],
        )
        # The registry is machine state under the primary checkout's data directory, which a worktree lacks.
        port = allocate_port(repo_root, load_all_records(worktree), None, is_bound)
        # Written before the manifest is read: an app with a record is read as someone else's, so tables its
        # manifest still carries about its publisher's workspace are not held to this one's tree.
        unversioned_record = AppRecord(
            name=name,
            package=package,
            port=port,
            origin=AppOrigin.INSTALLED,
            upstream_url=url,
            upstream_commit=commit,
            upstream_tree=run_git(repo_root, ["rev-parse", f"{commit}^{{tree}}"]),
        )
        write_record(worktree, unversioned_record)
        try:
            manifest = load_manifest(
                manifest_path(worktree, package), repo_root=worktree, mode=ManifestReadMode.TOLERANT
            )
        except AppManifestError as e:
            raise NotAnAppError(f"the {MANIFEST_FILENAME} of {url} cannot be read: {e}") from e
        collisions = find_collisions(worktree, package, manifest, frozenset())
        if collisions:
            raise CollisionError(collisions)
        record = unversioned_record.model_copy_update(
            to_update(unversioned_record.field_ref().upstream_version, manifest.version or "")
        )
        write_record(worktree, record)
        write_derived_files(worktree, package, derive_files(worktree, record, manifest))
        run_git(worktree, ["add", "--all"])
        run_git(worktree, ["commit", "--quiet", "-m", f"app-install: register {name}"])
        land_commit(repo_root, run_git(worktree, ["rev-parse", "HEAD"]))
    # Keeps the upstream commit's objects in the workspace repository for a later update or split to build on.
    run_git(repo_root, ["update-ref", f"{UPSTREAM_REF_PREFIX}{name}", commit])

    unresolved_environment = runtime.install_environment(manifest.environment)
    missing_secret_files = tuple(
        (SECRETS_DIRECTORY / f"{secret.file}.env").as_posix()
        for secret in manifest.secrets
        if not (repo_root / SECRETS_DIRECTORY / f"{secret.file}.env").is_file()
    )
    problems: list[str] = []
    reload_failure = runtime.reload_programs()
    if reload_failure is not None:
        problems.append(f"supervisord did not start the app's programs: {reload_failure}")
    # A program waiting on a secret file cannot answer yet, so its port is not waited for.
    is_answering = reload_failure is None and not missing_secret_files and runtime.wait_for_port(record.port)
    present_apps = workspace_app_names(repo_root)
    missing_dependencies = tuple(
        str(dependency.name) for dependency in manifest.dependencies if str(dependency.name) not in present_apps
    )
    return InstallOutcome(
        record=record,
        ref=ref,
        programs=program_names(manifest),
        is_answering=is_answering,
        missing_secret_files=missing_secret_files,
        missing_dependencies=missing_dependencies,
        unresolved_environment=unresolved_environment,
        manifest=manifest,
        problems=tuple(problems),
    )
