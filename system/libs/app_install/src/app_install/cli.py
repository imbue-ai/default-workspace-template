"""The ``app-install`` command.

Usage, from the workspace repo's root::

    uv run --no-sync app-install add <package> [--port <n>]
    uv run --no-sync app-install install <url> [--branch <ref>]
    uv run --no-sync app-install update <name> [--branch <ref>] [--continue]
    uv run --no-sync app-install regenerate [<name>]
    uv run --no-sync app-install uninstall <name>
    uv run --no-sync app-install migrate <package> [--dry-run]
    uv run --no-sync app-install link <name> [--refresh] [--create-dev-repo]
    uv run --no-sync app-install reference add <name> <path> [--note <text>]
    uv run --no-sync app-install reference remove <name> <path>
    uv run --no-sync app-install publish compare <name> [--commit <rev>]
    uv run --no-sync app-install publish set-version <name> <version>
    uv run --no-sync app-install publish assemble <name> [--commit <rev>]
    uv run --no-sync app-install publish push <name> [--repo-name <repository>] [--public]
    uv run --no-sync app-install publish visibility <name> [--make-public]
    uv run --no-sync app-install publish record <name>
    uv run --no-sync app-install verify <name> [--template <version>]
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

from app_install.errors import AppChecksFailedError
from app_install.errors import AppInstallError
from app_install.errors import CollisionError
from app_install.errors import LockCheckError
from app_install.errors import NoSnapshotError
from app_install.errors import NotAnAppError
from app_install.errors import NotAssembledError
from app_install.errors import NotPushedError
from app_install.errors import OutsideSkillsError
from app_install.errors import PublishedNameTakenError
from app_install.errors import PushRefusedError
from app_install.errors import RepositoryUnreachableError
from app_install.errors import SecretsFoundError
from app_install.installing import InstallOutcome
from app_install.installing import install_app
from app_install.linking import link_app
from app_install.migration import MigrateOutcome
from app_install.migration import migrate_app
from app_install.operations import add_app
from app_install.operations import check
from app_install.operations import install_lock
from app_install.operations import list_apps
from app_install.operations import regenerate
from app_install.operations import uninstall_app
from app_install.paths import secret_file_path
from app_install.ports import is_port_bound
from app_install.publishing import PublishedTree
from app_install.publishing import assemble_app
from app_install.publishing import compare_with_published
from app_install.publishing import published_visibility
from app_install.publishing import push_app
from app_install.publishing import record_publish
from app_install.publishing import set_version
from app_install.references import ReferenceOutcome
from app_install.references import add_reference
from app_install.references import remove_reference
from app_install.runtime import WorkspaceRuntime
from app_install.updating import UpdateOutcome
from app_install.updating import UpdateResult
from app_install.updating import update_app
from app_install.verifying import verify_app
from app_manifest.errors import AppManifestError
from app_manifest.manifest import AppManifest

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


def _adopter_needs(manifest: AppManifest) -> dict[str, object]:
    """What a manifest asks of whoever adopts the app: the same fields, in the same shapes, for an install's
    result and for a publish's."""
    return {
        "permissions": [note.model_dump(mode="json") for note in manifest.permissions],
        "secrets": [
            {
                "file": str(secret.file),
                "variables": [str(variable) for variable in secret.variables],
                "note": str(secret.note or ""),
            }
            for secret in manifest.secrets
        ],
        "llm": manifest.llm.model_dump(mode="json") if manifest.llm is not None else None,
        "adaptation": [note.model_dump(mode="json") for note in manifest.adaptation],
    }


def _install_fields(outcome: InstallOutcome) -> dict[str, object]:
    manifest = outcome.manifest
    missing_secret_files = set(outcome.missing_secret_files)
    needs = _adopter_needs(manifest)
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
            **needs,
            "secrets": [
                {
                    "file": str(secret.file),
                    "variables": [str(variable) for variable in secret.variables],
                    "note": str(secret.note or ""),
                    "is_present": secret_file_path(str(secret.file)).as_posix() not in missing_secret_files,
                }
                for secret in manifest.secrets
            ],
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
        removed_state_paths=list(outcome.removed_state_paths),
        kept_paths=list(outcome.kept_paths),
        kept_share_grants=(
            outcome.kept_share_grants.model_dump(mode="json") if outcome.kept_share_grants is not None else None
        ),
        kept_routine_state=list(outcome.kept_routine_state),
        kept_repositories=list(outcome.kept_repositories),
        dependents=list(outcome.dependents),
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


@main.command("link")
@click.argument("name")
@click.option(
    "--refresh",
    "is_refreshing",
    is_flag=True,
    help="Make a new join marker at the current commit, so later splits stay fast",
)
@click.option(
    "--create-dev-repo",
    "is_dev_repository_wanted",
    is_flag=True,
    help="Create the app's dev repository even though GitHub sync is not enabled for the workspace",
)
@click.pass_obj
def link_command(repo_root: Path, name: str, is_refreshing: bool, is_dev_repository_wanted: bool) -> None:
    """Make the first split of the app NAME's own history and, when GitHub sync is enabled, its private dev
    repository, which the sync service then keeps current. Run from the workspace's primary checkout."""
    with install_lock(repo_root):
        try:
            outcome = link_app(
                repo_root, name, is_refreshing, is_dev_repository_wanted, WorkspaceRuntime(repo_root=repo_root)
            )
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", name=name, detail=str(e))
    _emit(
        outcome.result.value,
        name=str(outcome.record.name),
        package=outcome.record.package,
        marker_commit=outcome.marker_commit,
        split_commit=outcome.split_commit,
        is_full_clone_used=outcome.is_full_clone_used,
        dev_repo_url=outcome.record.dev_repo_url,
        is_dev_repository_created=outcome.is_dev_repository_created,
        remaining=list(outcome.remaining),
        problems=list(outcome.problems),
    )


@main.command("verify")
@click.argument("name")
@click.option(
    "--template",
    "template_version",
    default=None,
    help="The template version to record (minds-v<major>.<minor>.<patch>); by default the one the checkout is on",
)
@click.pass_obj
def verify_command(repo_root: Path, name: str, template_version: str | None) -> None:
    """Run the checks of the app NAME and, when they pass, record the template version they passed on."""
    with install_lock(repo_root):
        try:
            outcome = verify_app(repo_root, name, template_version, WorkspaceRuntime(repo_root=repo_root))
        except AppChecksFailedError as e:
            _refuse("tests_failed", name=name, detail=str(e))
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", name=name, detail=str(e))
    _emit("verified", name=name, verified_template=outcome.verified_template, problems=list(outcome.problems))


@main.group("reference")
def reference_group() -> None:
    """Change what an app's record lists as using the app elsewhere in the workspace."""


def _reference_fields(outcome: ReferenceOutcome) -> dict[str, object]:
    return {
        "name": str(outcome.record.name),
        "path": outcome.reference.path,
        "note": outcome.reference.note,
        "references": [reference.model_dump(mode="json") for reference in outcome.record.references],
        "problems": list(outcome.problems),
    }


@reference_group.command("add")
@click.argument("name")
@click.argument("path")
@click.option("--note", default="", help="One line saying which of the app's surfaces the path uses")
@click.pass_obj
def reference_add_command(repo_root: Path, name: str, path: str, note: str) -> None:
    """List PATH, a file or directory outside the app NAME's directory, in the app's record as using the app."""
    with install_lock(repo_root):
        try:
            outcome = add_reference(repo_root, name, path, note, WorkspaceRuntime(repo_root=repo_root))
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", name=name, path=path, detail=str(e))
    _emit("reference_added", **_reference_fields(outcome))


@reference_group.command("remove")
@click.argument("name")
@click.argument("path")
@click.pass_obj
def reference_remove_command(repo_root: Path, name: str, path: str) -> None:
    """Take PATH out of what the record of the app NAME lists as using the app."""
    with install_lock(repo_root):
        try:
            outcome = remove_reference(repo_root, name, path, WorkspaceRuntime(repo_root=repo_root))
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", name=name, path=path, detail=str(e))
    _emit("reference_removed", **_reference_fields(outcome))


@main.group("publish")
def publish_group() -> None:
    """The steps of publishing an app to its repository, which the publish-app skill runs in order."""


def _published_tree_fields(published: PublishedTree) -> dict[str, object]:
    return {
        "source_commit": published.source_commit,
        "tree": published.tree,
        "paths": list(published.paths),
        "excluded_paths": list(published.excluded_paths),
    }


@publish_group.command("compare")
@click.argument("name")
@click.option("--commit", "revision", default="HEAD", show_default=True, help="The workspace commit to compare")
@click.pass_obj
def publish_compare_command(repo_root: Path, name: str, revision: str) -> None:
    """Say what a publish of the app NAME would push, how it differs from the version last published, and
    each line of it that names a skill the app does not carry."""
    try:
        outcome = compare_with_published(repo_root, name, revision)
    except (AppInstallError, AppManifestError) as e:
        _refuse("refused", name=name, detail=str(e))
    record = outcome.record
    _emit(
        "compared",
        name=str(record.name),
        origin=record.origin.value,
        is_published=bool(record.published_commit),
        published_repo_url=record.published_repo_url,
        published_commit=record.published_commit,
        published_version=record.published_version,
        version=outcome.version,
        changed_paths=[change.model_dump(mode="json") for change in outcome.changed_paths],
        outside_skill_uses=[use.model_dump(mode="json") for use in outcome.outside_skill_uses],
        is_outside_skills_checked=outcome.is_outside_skills_checked,
        **_published_tree_fields(outcome.published),
    )


@publish_group.command("set-version")
@click.argument("name")
@click.argument("version")
@click.pass_obj
def publish_set_version_command(repo_root: Path, name: str, version: str) -> None:
    """Write VERSION into the app NAME (its manifest and, for a Python app, its project and lock) with the
    template version this workspace is on. Commits nothing."""
    with install_lock(repo_root):
        try:
            outcome = set_version(repo_root, name, version, WorkspaceRuntime(repo_root=repo_root))
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", name=name, detail=str(e))
    _emit(
        "versioned",
        name=str(outcome.record.name),
        version=outcome.version,
        previous_version=outcome.previous_version,
        verified_template=outcome.verified_template,
        changed_paths=list(outcome.changed_paths),
        problems=list(outcome.problems),
    )


@publish_group.command("assemble")
@click.argument("name")
@click.option("--commit", "revision", default="HEAD", show_default=True, help="The workspace commit to publish")
@click.pass_obj
def publish_assemble_command(repo_root: Path, name: str, revision: str) -> None:
    """Build the tree a publish of the app NAME would push, outside the workspace, scan it for secrets, and
    check that it names no skill the app does not carry and that a Python app's lock holds there. Pushes
    nothing."""
    with install_lock(repo_root):
        try:
            outcome = assemble_app(repo_root, name, revision, WorkspaceRuntime(repo_root=repo_root))
        except SecretsFoundError as e:
            _refuse("secrets_found", name=name, findings=list(e.findings))
        except OutsideSkillsError as e:
            _refuse("outside_skills_found", name=name, uses=list(e.uses))
        except LockCheckError as e:
            _refuse("lock_check_failed", name=name, detail=str(e))
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", name=name, detail=str(e))
    manifest = outcome.manifest
    _emit(
        "assembled",
        name=str(outcome.record.name),
        version=manifest.version,
        verified_template=manifest.verified_template or "",
        is_first_publish=not outcome.record.published_commit,
        published_repo_url=outcome.record.published_repo_url,
        is_lock_checked=outcome.is_lock_checked,
        is_outside_skills_checked=outcome.is_outside_skills_checked,
        needs={
            **_adopter_needs(manifest),
            "environment": manifest.environment.model_dump(mode="json"),
            "dependencies": [dependency.model_dump(mode="json") for dependency in manifest.dependencies],
        },
        **_published_tree_fields(outcome.published),
    )


@publish_group.command("push")
@click.argument("name")
@click.option(
    "--repo-name",
    "repository_name",
    default=None,
    help="The repository a first publish creates; the app's name otherwise",
)
@click.option(
    "--public",
    "is_public",
    is_flag=True,
    help="Create the repository of a first publish public; it is private otherwise",
)
@click.pass_obj
def publish_push_command(repo_root: Path, name: str, repository_name: str | None, is_public: bool) -> None:
    """Push the assembled tree of the app NAME to its repository as one commit and tag it, creating the
    repository on the first publish, private unless --public is given. Never forced."""
    with install_lock(repo_root):
        try:
            outcome = push_app(repo_root, name, repository_name, is_public, WorkspaceRuntime(repo_root=repo_root))
        except PublishedNameTakenError as e:
            _refuse("name_taken", name=name, repo_name=repository_name or name, detail=str(e))
        except NotAssembledError as e:
            _refuse("not_assembled", name=name, detail=str(e))
        except PushRefusedError as e:
            _refuse("push_refused", name=name, detail=str(e))
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", name=name, detail=str(e))
    _emit(
        "pushed",
        name=str(outcome.record.name),
        published_repo_url=outcome.repo_url,
        published_commit=outcome.commit,
        parent=outcome.parent,
        version=outcome.version,
        tag=outcome.tag,
        is_repository_created=outcome.is_repository_created,
        is_private=outcome.is_private,
        is_tag_pushed=outcome.is_tag_pushed,
        derived_from=outcome.derived_from,
        problems=list(outcome.problems),
    )


@publish_group.command("visibility")
@click.argument("name")
@click.option(
    "--make-public",
    "is_made_public",
    is_flag=True,
    help="Make a private published repository public",
)
@click.pass_obj
def publish_visibility_command(repo_root: Path, name: str, is_made_public: bool) -> None:
    """Say whether the published repository of the app NAME is private, making it public first with
    --make-public."""
    try:
        outcome = published_visibility(repo_root, name, is_made_public, WorkspaceRuntime(repo_root=repo_root))
    except (AppInstallError, AppManifestError) as e:
        _refuse("refused", name=name, detail=str(e))
    _emit(
        "visibility",
        name=str(outcome.record.name),
        published_repo_url=outcome.repo_url,
        is_private=outcome.is_private,
        is_changed=outcome.is_changed,
    )


@publish_group.command("record")
@click.argument("name")
@click.pass_obj
def publish_record_command(repo_root: Path, name: str) -> None:
    """Write what was pushed for the app NAME into its record and commit that one file."""
    with install_lock(repo_root):
        try:
            outcome = record_publish(repo_root, name, WorkspaceRuntime(repo_root=repo_root))
        except NotAssembledError as e:
            _refuse("not_assembled", name=name, detail=str(e))
        except NotPushedError as e:
            _refuse("not_pushed", name=name, detail=str(e))
        except (AppInstallError, AppManifestError) as e:
            _refuse("refused", name=name, detail=str(e))
    record = outcome.record
    _emit(
        "recorded",
        name=str(record.name),
        published_repo_url=record.published_repo_url,
        published_commit=record.published_commit,
        published_version=record.published_version,
        verified_template=record.verified_template,
        problems=list(outcome.problems),
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
    """Print every user app's record with its state (owned, installed, or modified) and whether it is linked."""
    try:
        apps = list_apps(repo_root)
    except (AppInstallError, AppManifestError) as e:
        _refuse("refused", detail=str(e))
    _emit(
        "listed",
        apps=[
            {**app.record.model_dump(mode="json"), "state": app.state.value, "is_linked": app.is_linked}
            for app in apps
        ],
    )
