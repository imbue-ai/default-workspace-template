"""Build the web latchkey auth sandbox's Modal image and print its id.

The image is the product ``system/Dockerfile`` with a workspace's first boot and the Screen's stack baked in,
plus what a browser sign-in needs (the web latchkey auth sandbox spec, section 5.1): the pinned latchkey CLI
checked, this library in its own environment, the template ``config.json`` naming the sign-in browser script
(the Fortress launcher, its window at the display's corner), the sandbox's boot script and supervisord config at
``/opt/auth-sandbox/``, and the ``auth-sandbox-*`` commands on PATH. The steps themselves are
``auth_sandbox.image_build`` (tested over a fake sandbox); this script runs them in a real Modal sandbox made
from the product image and snapshots its filesystem.

Nothing user-specific goes into the image. The connector creates each auth sandbox from the printed id
(``MINDS_AUTH_SANDBOX_IMAGE_ID`` on the tier) and writes the user's material in afterwards.

Run it from the repo root with Modal credentials for the target workspace (an activated minds env, or a
``MODAL_TOKEN_ID``/``MODAL_TOKEN_SECRET`` pair), from this library's own project; ``modal`` is not a dependency,
so it comes in for the one run::

    uv sync --locked --project system/libs/auth_sandbox
    uv run --no-sync --project system/libs/auth_sandbox --with modal python -m auth_sandbox.modal_image_build [--environment-name NAME]

The Dockerfile must carry no BuildKit secret mount, which is the case whenever ``pyproject.toml`` pins mngr
from the public mirror (``python system/scripts/set_mngr_pin.py --check`` says ``public``).
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

import click
import modal
from modal.stream_type import StreamType
from pydantic import ConfigDict, Field

from auth_sandbox.image_build import (
    BuildSandboxInterface,
    ImageBuildSettings,
    pinned_latchkey_version,
    run_build,
)

_DOCKERFILE = Path("system") / "Dockerfile"
_SETUP_SYSTEM = Path("system") / "scripts" / "setup_system.sh"
_DEFAULT_APP_NAME = "auth-sandbox-image-build"
# The build sandbox's own bound: the Screen unit's downloads dominate, and a build that takes longer than
# this is one that is already failing.
_BUILD_SANDBOX_TIMEOUT_SECONDS = 3 * 3600
_BUILD_CPU = 2.0
_BUILD_MEMORY_MB = 4096


class ModalBuildSandbox(BuildSandboxInterface):
    """A Modal sandbox as the build's target: commands over ``exec``, the snapshot through the SDK."""

    # ``modal.Sandbox`` is not a pydantic type.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sandbox: modal.Sandbox = Field(
        frozen=True, description="The sandbox made from the product image"
    )

    def run(self, command: str, timeout_seconds: int) -> int:
        # stderr is merged into stdout so one pipe is drained in order and nothing deadlocks on a full
        # buffer; the command line is echoed above its output for the build log.
        click.echo(f"\n=== {command} ===")
        process = self.sandbox.exec(
            "bash", "-lc", command, timeout=timeout_seconds, stderr=StreamType.STDOUT
        )
        for line in process.stdout:
            click.echo(line, nl=False)
        return process.wait()

    def snapshot(self, timeout_seconds: int) -> str:
        image = self.sandbox.snapshot_filesystem(timeout=timeout_seconds)
        return image.object_id


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--app-name",
        default=_DEFAULT_APP_NAME,
        help=f"The Modal app the build sandbox runs in (default: {_DEFAULT_APP_NAME!r}).",
    )
    parser.add_argument(
        "--environment-name",
        default=os.environ.get("MODAL_ENVIRONMENT"),
        help="The Modal environment to build in (default: $MODAL_ENVIRONMENT, else the profile's).",
    )
    parser.add_argument(
        "--image-id-output",
        type=Path,
        default=None,
        help="Also write the image id (bare, no newline) to this file once the snapshot succeeds.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    repo_root = Path.cwd()
    if not (repo_root / _DOCKERFILE).is_file():
        click.echo(
            f"Run this from the template's repo root: no {_DOCKERFILE} under {repo_root}",
            err=True,
        )
        return 2
    settings = ImageBuildSettings(
        latchkey_version=pinned_latchkey_version(
            (repo_root / _SETUP_SYSTEM).read_text()
        )
    )
    with modal.enable_output():
        image = modal.Image.from_dockerfile(
            str(repo_root / _DOCKERFILE), context_dir=str(repo_root)
        )
        app = modal.App.lookup(
            args.app_name,
            create_if_missing=True,
            environment_name=args.environment_name,
        )
        sandbox = modal.Sandbox.create(
            image=image,
            app=app,
            timeout=_BUILD_SANDBOX_TIMEOUT_SECONDS,
            cpu=_BUILD_CPU,
            memory=_BUILD_MEMORY_MB,
        )
        try:
            image_id = run_build(ModalBuildSandbox(sandbox=sandbox), settings)
        finally:
            sandbox.terminate()
    if args.image_id_output is not None:
        args.image_id_output.write_text(image_id)
    click.echo(f"\nAuth sandbox image: {image_id}")
    click.echo(
        f"Set MINDS_AUTH_SANDBOX_IMAGE_ID={shlex.quote(image_id)} on the tier to use it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
