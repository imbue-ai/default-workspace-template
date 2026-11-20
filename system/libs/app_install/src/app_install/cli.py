"""The ``app-install`` command.

Usage, from the workspace repo's root::

    uv run --no-sync app-install add <package> [--port <n>]
    uv run --no-sync app-install install <url> [--branch <ref>]
    uv run --no-sync app-install update <name> [--branch <ref>] [--continue]
    uv run --no-sync app-install regenerate [<name>]
    uv run --no-sync app-install uninstall <name>
    uv run --no-sync app-install migrate <package> [--dry-run]
    uv run --no-sync app-install check
    uv run --no-sync app-install list

The command is deterministic and asks nothing. Every run ends in one JSON line on stdout that
says what happened, with ``"result"`` naming the outcome, so a skill, the update pass, or the
Imbue Studio app can read it. A run that changed nothing because it was refused exits 1.
"""

import json
from pathlib import Path
from typing import Final

import click

from app_install.errors import AppInstallError
from app_install.errors import CollisionError
from app_install.errors import NoSnapshotError
from app_install.errors import NotAnAppError
from app_install.errors import RepositoryUnreachableError
from app_install.installing import InstallOutcome
from app_install.installing import install_app
from app_install.migration import MigrateOutcome
from app_install.migration import migrate_app
from app_install.operations import add_app
from app_install.operations import check
from app_install.operations import install_lock
from app_install.operations import list_apps
from app_install.operations import regenerate
from app_install.operations import uninstall_app
from app_install.ports import is_port_bound
from app_install.runtime import WorkspaceRuntime
from app_install.updating import UpdateOutcome
from app_install.updating import UpdateResult
from app_install.updating import update_app
from app_manifest.errors import AppManifestError

_EXIT_REFUSED: Final[int] = 1


def _emit(result: str, **fields: object) -> None:
    click.echo(json.dumps({"result": result, **fields}, sort_keys=True))


def _refuse(result: str, **fields: object) -> None:
    _emit(result, **fields)
    raise click.exceptions.Exit(_EXIT_REFUSED)


@click.group()
@click.option(
    "--repo-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="The workspace repo's root",
)
@click.pass_context
def main(context: click.Context, repo_root: Path) -> None:
    """Register, regenerate, and check the apps a workspace's agent builds or installs."""
    context.obj = repo_root.resolve()


@main.command("add")
@click.argument("package")
@click.option(
    "--port", type=int, default=None, help="The loopback port to give the app; the lowest free one otherwise"
)
@click.pass_obj
def add_command(repo_root: Path, package: str, port: int | None) -> None:
    """Register the app directory system/apps/PACKAGE: allocate its port, write its record, generate its files."""
    with install_lock(repo_root):
        try:
            outcome = add_app(repo_root, package, port, is_port_bound)
        except CollisionError as e:
            _refuse("collision", package=package, problems=list(e.problems))
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", package=package, detail=str(e))
    _emit(
        "added",
        name=str(outcome.record.name),
        package=outcome.record.package,
        port=outcome.record.port,
        changed_paths=list(outcome.changed_paths),
    )


def _install_fields(outcome: InstallOutcome) -> dict[str, object]:
    manifest = outcome.manifest
    missing_secret_files = set(outcome.missing_secret_files)
    return {
        "name": str(outcome.record.name),
        "package": outcome.record.package,
        "port": outcome.record.port,
        "ref": outcome.ref,
        "version": outcome.record.upstream_version,
        "upstream_url": outcome.record.upstream_url,
        "upstream_commit": outcome.record.upstream_commit,
        "programs": list(outcome.programs),
        "is_answering": outcome.is_answering,
        "needs": {
            "permissions": [note.model_dump(mode="json") for note in manifest.permissions],
            "secrets": [
                {
                    "file": str(secret.file),
                    "variables": [str(variable) for variable in secret.variables],
                    "note": str(secret.note or ""),
                    "is_present": f"data/.secrets/{secret.file}.env" not in missing_secret_files,
                }
                for secret in manifest.secrets
            ],
            "llm": manifest.llm.model_dump(mode="json") if manifest.llm is not None else None,
            "adaptation": [note.model_dump(mode="json") for note in manifest.adaptation],
            "missing_dependencies": list(outcome.missing_dependencies),
            "unresolved_environment": list(outcome.unresolved_environment),
        },
        "problems": list(outcome.problems),
    }


@main.command("install")
@click.argument("url")
@click.option("--branch", "ref", default=None, help="The tag or branch to install; the latest version otherwise")
@click.pass_obj
def install_command(repo_root: Path, url: str, ref: str | None) -> None:
    """Install the app published at URL: add it to the workspace, register it, and start its programs."""
    with install_lock(repo_root):
        try:
            outcome = install_app(repo_root, url, ref, WorkspaceRuntime(repo_root=repo_root), is_port_bound)
        except CollisionError as e:
            _refuse("collision", url=url, problems=list(e.problems))
        except NotAnAppError as e:
            _refuse("not_an_app", url=url, detail=str(e))
        except RepositoryUnreachableError as e:
            _refuse("needs_github_access" if e.is_access_needed else "unreachable", url=url, detail=str(e))
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", url=url, detail=str(e))
    _emit("installed", **_install_fields(outcome))


def _update_fields(outcome: UpdateOutcome) -> dict[str, object]:
    return {
        "name": str(outcome.record.name),
        "ref": outcome.ref,
        "previous_version": outcome.previous_version,
        "version": outcome.record.upstream_version,
        "upstream_commit": outcome.record.upstream_commit,
        "conflicted_paths": list(outcome.conflicted_paths),
        "worktree": outcome.worktree,
        "snapshot": outcome.snapshot,
        "detail": outcome.detail,
        "problems": list(outcome.problems),
    }


@main.command("update")
@click.argument("name")
@click.option("--branch", "ref", default=None, help="The tag or branch to update to; the latest version otherwise")
@click.option(
    "--continue", "is_continuing", is_flag=True, help="Finish an update that stopped on a conflict or failing checks"
)
@click.pass_obj
def update_command(repo_root: Path, name: str, ref: str | None, is_continuing: bool) -> None:
    """Update the installed app NAME from the repository it came from, over the workspace's own changes to it."""
    with install_lock(repo_root):
        try:
            outcome = update_app(repo_root, name, ref, is_continuing, WorkspaceRuntime(repo_root=repo_root))
        except CollisionError as e:
            _refuse("collision", name=name, problems=list(e.problems))
        except RepositoryUnreachableError as e:
            _refuse("needs_github_access" if e.is_access_needed else "unreachable", name=name, detail=str(e))
        except NoSnapshotError as e:
            _refuse("no_snapshot", name=name, detail=str(e))
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", name=name, detail=str(e))
    if outcome.result in (UpdateResult.CONFLICT, UpdateResult.TESTS_FAILED):
        _refuse(outcome.result.value, **_update_fields(outcome))
    _emit(outcome.result.value, **_update_fields(outcome))


@main.command("regenerate")
@click.argument("name", required=False)
@click.pass_obj
def regenerate_command(repo_root: Path, name: str | None) -> None:
    """Rewrite the derived files of the app NAME, or of every app, and resolve duplicate ports."""
    with install_lock(repo_root):
        try:
            outcome = regenerate(repo_root, name, is_port_bound)
        except CollisionError as e:
            _refuse("collision", problems=list(e.problems))
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", detail=str(e))
    _emit(
        "regenerated",
        changed_paths=list(outcome.changed_paths),
        reassigned_ports=dict(outcome.port_by_reassigned_name),
    )


@main.command("uninstall")
@click.argument("name")
@click.pass_obj
def uninstall_command(repo_root: Path, name: str) -> None:
    """Remove the app NAME and its generated files, keeping its stored data and its secret files."""
    with install_lock(repo_root):
        try:
            outcome = uninstall_app(repo_root, name, WorkspaceRuntime(repo_root=repo_root))
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", name=name, detail=str(e))
    _emit(
        "uninstalled",
        name=outcome.name,
        removed_paths=list(outcome.removed_paths),
        kept_paths=list(outcome.kept_paths),
        problems=list(outcome.problems),
    )


def _migration_fields(outcome: MigrateOutcome) -> dict[str, object]:
    plan = outcome.plan
    return {
        "name": plan.name,
        "package": plan.package,
        "port": plan.port,
        "moves": [list(move) for move in plan.moves],
        "references": [reference.model_dump(mode="json") for reference in plan.references],
        "notes": list(plan.notes),
        "problems": list(outcome.problems),
    }


@main.command("migrate")
@click.argument("package")
@click.option("--dry-run", is_flag=True, help="Say what the conversion would do and change nothing")
@click.pass_obj
def migrate_command(repo_root: Path, package: str, dry_run: bool) -> None:
    """Convert the app at system/apps/PACKAGE, built before apps kept everything in their directory, into a user
    app with a record; an app that cannot be converted as it is is left untouched."""
    with install_lock(repo_root):
        try:
            outcome = migrate_app(repo_root, package, is_port_bound, dry_run)
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", package=package, detail=str(e))
    if outcome.problems:
        _refuse("unconverted", **_migration_fields(outcome))
    _emit("would_migrate" if dry_run else "migrated", **_migration_fields(outcome))


@main.command("check")
@click.pass_obj
def check_command(repo_root: Path) -> None:
    """Fail when a derived file differs from what regenerate would write, or a uniqueness rule is broken."""
    try:
        problems = check(repo_root)
    except (AppInstallError, AppManifestError) as e:
        _refuse("refused", detail=str(e))
    if problems:
        _refuse("inconsistent", problems=list(problems))
    _emit("consistent")


@main.command("list")
@click.pass_obj
def list_command(repo_root: Path) -> None:
    """Print every user app's record and whether it was built here or installed."""
    try:
        apps = list_apps(repo_root)
    except (AppInstallError, AppManifestError) as e:
        _refuse("refused", detail=str(e))
    _emit("listed", apps=[{**app.record.model_dump(mode="json"), "state": app.state} for app in apps])
