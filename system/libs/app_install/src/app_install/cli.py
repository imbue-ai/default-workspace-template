"""The ``app-install`` command.

Usage, from the workspace repo's root::

    uv run --no-sync app-install add <package> [--port <n>]
    uv run --no-sync app-install regenerate [<name>]
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
from app_install.operations import add_app
from app_install.operations import check
from app_install.operations import install_lock
from app_install.operations import list_apps
from app_install.operations import regenerate
from app_install.ports import is_port_bound
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


@main.command("regenerate")
@click.argument("name", required=False)
@click.pass_obj
def regenerate_command(repo_root: Path, name: str | None) -> None:
    """Rewrite the derived files of the app NAME, or of every app, and resolve duplicate ports."""
    with install_lock(repo_root):
        try:
            outcome = regenerate(repo_root, name, is_port_bound)
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", detail=str(e))
    _emit(
        "regenerated",
        changed_paths=list(outcome.changed_paths),
        reassigned_ports=dict(outcome.port_by_reassigned_name),
    )


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
