import json
from pathlib import Path

import click
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from pydantic import Field

from app_manifest.errors import AppManifestError
from app_manifest.manifest import AppManifest
from app_manifest.manifest import load_manifest
from app_manifest.primitives import ReferenceNote
from app_manifest.primitives import ReferencePath
from app_manifest.primitives import RepoRelativePath
from app_manifest.scope import CreationScope
from app_manifest.scope import ManifestReferenceMatch
from app_manifest.scope import compute_app_scope
from app_manifest.scope import compute_skill_scope
from app_manifest.scope import find_referencing_manifests
from app_manifest.scope import render_scope_file
from app_manifest.scope import with_diff_against_base


class ReferenceLookupRow(FrozenModel):
    """One line of ``app-manifest references --for-path``: an app that declares it owns the target."""

    app: NonEmptyStr = Field(description="The referencing app's registered name")
    manifest: RepoRelativePath = Field(description="That app's manifest")
    path: ReferencePath = Field(description="The reference entry that covers the target")
    note: ReferenceNote | None = Field(description="The note the manifest wrote on that entry")


@click.group()
def app_manifest_cli() -> None:
    """Inspect and validate workspace app manifests."""


@app_manifest_cli.command("validate-manifest")
@click.argument("manifest_path", type=click.Path(path_type=Path))
def validate_manifest(manifest_path: Path) -> None:
    """Validate an app.toml (and that the icon it names exists); exit non-zero with the reason otherwise."""
    try:
        manifest = load_manifest(manifest_path)
    except AppManifestError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"ok: {manifest.name} ({manifest.display_name})")


@app_manifest_cli.command("footprint")
@click.argument("manifest_path", type=click.Path(path_type=Path), required=False)
@click.option(
    "--for-path",
    "for_path",
    default=None,
    help="Compute a skill footprint for this repo-root-relative path instead of an app's manifest.",
)
@click.option(
    "--repo-root",
    "repo_root",
    type=click.Path(path_type=Path),
    default=None,
    help="The repo root every path is relative to (default: the current directory).",
)
@click.option(
    "--diff-base",
    "diff_base",
    default=None,
    help="A git ref to diff HEAD against; the scope file then reports the changes outside the footprint.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write the scope file here (parent directories are created); default: stdout.",
)
def footprint(
    manifest_path: Path | None,
    for_path: str | None,
    repo_root: Path | None,
    diff_base: str | None,
    out_path: Path | None,
) -> None:
    """Write the scope file for one creation: its own paths, its wiring, and what it references."""
    if manifest_path is not None and for_path is not None:
        raise click.UsageError("pass either a manifest path or --for-path, not both")
    resolved_repo_root = (repo_root if repo_root is not None else Path.cwd()).resolve()
    try:
        if manifest_path is not None:
            scope = _app_scope(resolved_repo_root, manifest_path)
        elif for_path is not None:
            scope = compute_skill_scope(resolved_repo_root, RepoRelativePath(for_path))
        else:
            raise click.UsageError("pass either a manifest path or --for-path")
        described_scope = (
            scope
            if diff_base is None
            else with_diff_against_base(scope, resolved_repo_root, diff_base)
        )
    except AppManifestError as e:
        raise click.ClickException(str(e)) from e
    _emit_scope_file(described_scope, out_path)


def _app_scope(repo_root: Path, manifest_path: Path) -> CreationScope:
    resolved_manifest_path = manifest_path if manifest_path.is_absolute() else repo_root / manifest_path
    manifest = load_manifest(resolved_manifest_path, repo_root=repo_root)
    return compute_app_scope(repo_root, resolved_manifest_path, manifest)


def _emit_scope_file(scope: CreationScope, out_path: Path | None) -> None:
    rendered = render_scope_file(scope)
    if out_path is None:
        click.echo(rendered, nl=False)
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    click.echo(f"wrote {out_path}")


@app_manifest_cli.command("references")
@click.option(
    "--for-path",
    "for_path",
    required=True,
    help="The repo-root-relative path to look up; one JSON object per referencing app is printed.",
)
@click.option(
    "--repo-root",
    "repo_root",
    type=click.Path(path_type=Path),
    default=None,
    help="The repo root every path is relative to (default: the current directory).",
)
def references(for_path: str, repo_root: Path | None) -> None:
    """Print every app whose manifest declares that it owns this path; nothing at all when none does."""
    resolved_repo_root = (repo_root if repo_root is not None else Path.cwd()).resolve()
    try:
        matches = find_referencing_manifests(resolved_repo_root, RepoRelativePath(for_path))
    except AppManifestError as e:
        raise click.ClickException(str(e)) from e
    for match in matches:
        row = _lookup_row(resolved_repo_root, match)
        click.echo(json.dumps(row.model_dump(mode="json")))


def _lookup_row(repo_root: Path, match: ManifestReferenceMatch) -> ReferenceLookupRow:
    manifest: AppManifest = match.manifest
    return ReferenceLookupRow(
        app=NonEmptyStr(manifest.name),
        manifest=RepoRelativePath(match.manifest_path.resolve().relative_to(repo_root).as_posix()),
        path=match.reference.path,
        note=match.reference.note,
    )


def main() -> None:
    app_manifest_cli()
