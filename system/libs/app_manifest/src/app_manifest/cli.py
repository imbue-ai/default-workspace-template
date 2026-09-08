from pathlib import Path

import click

from app_manifest.errors import AppManifestError
from app_manifest.manifest import load_manifest


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


def main() -> None:
    app_manifest_cli()
