"""What the app-install command does: register an app, regenerate its derived files, check them, list the apps."""

import fcntl
import shutil
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from loguru import logger
from pydantic import Field

from app_install.collisions import find_collisions
from app_install.collisions import find_skill_collisions
from app_install.derived import DerivedFiles
from app_install.derived import derive_files
from app_install.derived import describe_differences
from app_install.derived import existing_derived_paths
from app_install.derived import write_derived_files
from app_install.errors import AppAlreadyRegisteredError
from app_install.errors import AppNotFoundError
from app_install.errors import CollisionError
from app_install.errors import GitCommandError
from app_install.git import run_git
from app_install.interfaces import WorkspaceRuntimeInterface
from app_install.paths import APP_DATA_DIRECTORY
from app_install.paths import INSTALL_LOCK_PATH
from app_install.paths import UPSTREAM_REF_PREFIX
from app_install.paths import app_directory
from app_install.paths import manifest_path
from app_install.paths import secret_file_path
from app_install.ports import allocate_port
from app_manifest.errors import AppManifestError
from app_manifest.manifest import AppManifest
from app_manifest.manifest import ManifestReadMode
from app_manifest.manifest import check_declares_no_record_tables
from app_manifest.manifest import load_manifest
from app_manifest.records import AppOrigin
from app_manifest.records import AppRecord
from app_manifest.records import load_all_records
from app_manifest.records import load_record
from app_manifest.records import record_path
from app_manifest.records import write_record

# A record no commit holds yet sorts after every committed one, so it is the one a duplicate port is taken from.
_UNCOMMITTED_SORT_TIME: Final[int] = 2**62


class AddOutcome(FrozenModel):
    """What registering an app did."""

    record: AppRecord = Field(description="The record written for the app")
    changed_paths: tuple[str, ...] = Field(description="The record and every derived file written, repo-relative")


class RegenerateOutcome(FrozenModel):
    """What a regenerate changed."""

    changed_paths: tuple[str, ...] = Field(description="Every file rewritten or removed, repo-relative")
    port_by_reassigned_name: dict[str, int] = Field(
        description="The new port of each app that shared one with an app whose record is older"
    )


class UninstallOutcome(FrozenModel):
    """What removing an app did, and what it left for the user."""

    name: str = Field(description="The app that was removed")
    removed_paths: tuple[str, ...] = Field(description="Every path removed from the tree, repo-relative")
    kept_paths: tuple[str, ...] = Field(
        description="What was left on purpose: the app's stored data and its secret files, when it has any"
    )
    problems: tuple[str, ...] = Field(
        description="What could not be done outside the tree (programs not reloaded, registry row not removed, "
        "upstream ref not deleted, removal not committed); the app is removed from the tree all the same"
    )


class ListedApp(FrozenModel):
    """One user app as ``app-install list`` reports it."""

    record: AppRecord = Field(description="The app's record")
    state: str = Field(description="owned for an app built here, installed for one added from elsewhere")


@contextmanager
def install_lock(repo_root: Path) -> Generator[None, None, None]:
    """Hold the workspace's one installer lock, so two runs never write records and derived files at once."""
    lock_path = repo_root / INSTALL_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def load_app_manifest(repo_root: Path, package: str, origin: AppOrigin) -> AppManifest:
    """The manifest of the app at ``system/apps/<package>``; raises AppNotFoundError when there is none.

    An app this workspace built is read strictly, since its manifest is this workspace's to get right. An
    installed app is read tolerantly: its publisher may run a newer template, whose keys this one does not know.
    """
    path = manifest_path(repo_root, package)
    if not path.is_file():
        raise AppNotFoundError(f"no app at {app_directory(repo_root, package).relative_to(repo_root).as_posix()}")
    mode = ManifestReadMode.STRICT if origin is AppOrigin.LOCAL else ManifestReadMode.TOLERANT
    return load_manifest(path, repo_root=repo_root, mode=mode)


def add_app(
    repo_root: Path,
    package: str,
    requested_port: int | None,
    is_bound: Callable[[int], bool],
) -> AddOutcome:
    """Register the app directory ``system/apps/<package>`` as an app this workspace built: allocate its port,
    write its record, and generate its derived files.

    Raises AppNotFoundError for a directory with no manifest, AppAlreadyRegisteredError for an app that has a
    record, ManifestLoadError for a manifest that carries the tables an app with a record does not declare,
    CollisionError when it would take a name something in the workspace holds, and PortUnavailableError for a
    requested port that is taken. Nothing is written when it raises.
    """
    manifest = load_app_manifest(repo_root, package, AppOrigin.LOCAL)
    records = load_all_records(repo_root)
    if any(record.package == package for record in records):
        raise AppAlreadyRegisteredError(f"the app at system/apps/{package} already has a record")
    # The app has no record yet, so its manifest was read as a built-in app's, which may carry these tables.
    check_declares_no_record_tables(manifest_path(repo_root, package), manifest)
    collisions = find_collisions(repo_root, package, manifest, frozenset())
    if collisions:
        raise CollisionError(collisions)
    port = allocate_port(repo_root, records, requested_port, is_bound)
    record = AppRecord(name=manifest.name, package=package, port=port, origin=AppOrigin.LOCAL)
    written_record_path = write_record(repo_root, record)
    changed = write_derived_files(repo_root, package, derive_files(repo_root, record, manifest))
    return AddOutcome(record=record, changed_paths=(written_record_path.relative_to(repo_root).as_posix(), *changed))


def _first_commit_time(repo_root: Path, relative_path: str) -> int:
    """When the file was first committed, as a Unix time; a file no commit holds sorts last."""
    try:
        listing = run_git(repo_root, ["log", "--diff-filter=A", "--format=%ct", "--", relative_path])
    except GitCommandError as e:
        logger.warning("Could not read when {} was committed, so it is treated as uncommitted: {}", relative_path, e)
        return _UNCOMMITTED_SORT_TIME
    times = [int(line) for line in listing.split() if line.isdigit()]
    return min(times) if times else _UNCOMMITTED_SORT_TIME


def _resolve_duplicate_ports(
    repo_root: Path, records: tuple[AppRecord, ...], is_bound: Callable[[int], bool]
) -> tuple[tuple[AppRecord, ...], dict[str, int]]:
    """The records with a new port for every app that shares one with an app whose record was committed earlier,
    and each new port by the app's name: two branches can each allocate the same port, since each sees only its
    own worktree. Nothing is written."""
    ordered = sorted(
        records,
        key=lambda record: (
            _first_commit_time(repo_root, record_path(repo_root, record.name).relative_to(repo_root).as_posix()),
            str(record.name),
        ),
    )
    kept: list[AppRecord] = []
    reassigned: dict[str, int] = {}
    for record in ordered:
        if all(record.port != other.port for other in kept):
            kept.append(record)
            continue
        new_port = allocate_port(repo_root, tuple(kept) + tuple(records), None, is_bound)
        kept.append(record.model_copy_update(to_update(record.field_ref().port, new_port)))
        reassigned[str(record.name)] = new_port
    return tuple(sorted(kept, key=lambda record: str(record.name))), reassigned


def _skill_link_collisions(repo_root: Path, derived_by_record: Sequence[tuple[AppRecord, DerivedFiles]]) -> list[str]:
    """Why the skill links of the apps about to be regenerated cannot all be written: one would replace a skill
    the workspace or another app already holds under that name, or two of the apps carry a skill of one name."""
    problems: list[str] = []
    claimant_by_link: dict[str, str] = {}
    for record, derived in derived_by_record:
        own_derived_paths = set(existing_derived_paths(repo_root, record.package))
        problems.extend(
            f"{record.name}: {problem}"
            for problem in find_skill_collisions(repo_root, record.package, own_derived_paths)
        )
        for relative_link in sorted(derived.link_target_by_path):
            claimant = claimant_by_link.setdefault(relative_link, str(record.name))
            # A link that is there already has its one holder, and the other app is refused above.
            is_link_new = not (repo_root / relative_link).is_symlink() and not (repo_root / relative_link).exists()
            if is_link_new and claimant != str(record.name):
                problems.append(f"{record.name}: {relative_link} is also the link to a skill of {claimant}")
    return problems


def regenerate(repo_root: Path, name: str | None, is_bound: Callable[[int], bool]) -> RegenerateOutcome:
    """Rewrite the derived files of the app registered as ``name``, or of every app, after giving a new port to any
    app that shares one.

    Raises AppNotFoundError for a name with no record, and CollisionError when a skill an app carries would take
    the place of one the workspace or another app holds. Nothing is written when it raises.
    """
    recorded = load_all_records(repo_root)
    if name is not None and all(str(record.name) != name for record in recorded):
        raise AppNotFoundError(f"no app record for {name!r}")
    records, reassigned = _resolve_duplicate_ports(repo_root, recorded, is_bound)
    derived_by_record = [
        (record, derive_files(repo_root, record, load_app_manifest(repo_root, record.package, record.origin)))
        for record in records
        if name is None or str(record.name) == name or str(record.name) in reassigned
    ]
    collisions = _skill_link_collisions(repo_root, derived_by_record)
    if collisions:
        raise CollisionError(tuple(collisions))
    changed: list[str] = [
        write_record(repo_root, record).relative_to(repo_root).as_posix()
        for record, _derived in derived_by_record
        if str(record.name) in reassigned
    ]
    for record, derived in derived_by_record:
        changed.extend(write_derived_files(repo_root, record.package, derived))
    return RegenerateOutcome(changed_paths=tuple(changed), port_by_reassigned_name=reassigned)


def check(repo_root: Path) -> tuple[str, ...]:
    """Every way the tree departs from what the records and manifests call for; empty when it is consistent.

    A derived file that differs from what the generator writes, an app whose manifest cannot be read, two apps on
    one port, and an app that collides with something else in the workspace are each a problem.
    """
    problems: list[str] = []
    records = load_all_records(repo_root)
    names_by_port: dict[int, list[str]] = {}
    for record in records:
        names_by_port.setdefault(record.port, []).append(str(record.name))
        try:
            manifest = load_app_manifest(repo_root, record.package, record.origin)
        except (AppNotFoundError, AppManifestError) as e:
            problems.append(f"{record.name}: {e}")
            continue
        if manifest.name != record.name:
            problems.append(
                f"{record.name}: its record names the app {str(record.name)!r} but its manifest says "
                f"{str(manifest.name)!r}"
            )
            continue
        problems.extend(
            f"{record.name}: {problem}"
            for problem in find_collisions(repo_root, record.package, manifest, frozenset())
        )
        derived = derive_files(repo_root, record, manifest)
        problems.extend(
            f"{record.name}: {difference}" for difference in describe_differences(repo_root, record.package, derived)
        )
    for port, names in sorted(names_by_port.items()):
        if len(names) > 1:
            problems.append(f"port {port} is recorded for more than one app: {', '.join(sorted(names))}")
    return tuple(problems)


def uninstall_app(repo_root: Path, name: str, runtime: WorkspaceRuntimeInterface) -> UninstallOutcome:
    """Remove the app registered as ``name``: its directory, its record, and its derived files, then have
    supervisord drop its programs, remove its registry row, and commit the removal.

    The app's stored data under ``data/.apps/<name>/`` and its secret files are kept, and its repositories are
    left alone. Raises AppRecordNotFoundError when no app is registered under the name.
    """
    record = load_record(repo_root, name)
    directory = app_directory(repo_root, record.package)
    removed: list[str] = list(existing_derived_paths(repo_root, record.package))
    for relative_path in removed:
        (repo_root / relative_path).unlink()
    if directory.is_dir():
        secret_files = _declared_secret_files(repo_root, record)
        shutil.rmtree(directory)
        removed.append(directory.relative_to(repo_root).as_posix())
    else:
        secret_files = ()
    record_file = record_path(repo_root, name)
    record_file.unlink()
    removed.append(record_file.relative_to(repo_root).as_posix())

    problems: list[str] = []
    reload_failure = runtime.reload_programs()
    if reload_failure is not None:
        problems.append(f"supervisord did not drop the app's programs: {reload_failure}")
    registry_failure = runtime.remove_registry_row(name)
    if registry_failure is not None:
        problems.append(f"the app's registry row was not removed: {registry_failure}")
    ref_failure = runtime.delete_ref(f"{UPSTREAM_REF_PREFIX}{name}")
    if ref_failure is not None:
        problems.append(f"the app's upstream ref was not deleted: {ref_failure}")
    commit_failure = runtime.commit_paths(removed, f"app-install: uninstall {name}")
    if commit_failure is not None:
        problems.append(f"the removal was not committed: {commit_failure}")

    kept = [
        path.relative_to(repo_root).as_posix()
        for path in (repo_root / APP_DATA_DIRECTORY / name, *(repo_root / file for file in secret_files))
        if path.exists()
    ]
    return UninstallOutcome(name=name, removed_paths=tuple(removed), kept_paths=tuple(kept), problems=tuple(problems))


def _declared_secret_files(repo_root: Path, record: AppRecord) -> tuple[Path, ...]:
    """The secret files the app's manifest declares, read before its directory goes; none when the manifest
    cannot be read, since an app that cannot be read can still be removed."""
    try:
        manifest = load_app_manifest(repo_root, record.package, AppOrigin.INSTALLED)
    except (AppNotFoundError, AppManifestError) as e:
        logger.warning("Could not read the manifest of {} to name its secret files: {}", record.name, e)
        return ()
    return tuple(secret_file_path(str(secret.file)) for secret in manifest.secrets)


def list_apps(repo_root: Path) -> tuple[ListedApp, ...]:
    """Every user app of the workspace with whether it was built here or installed."""
    return tuple(
        ListedApp(record=record, state="owned" if record.origin is AppOrigin.LOCAL else "installed")
        for record in load_all_records(repo_root)
    )
