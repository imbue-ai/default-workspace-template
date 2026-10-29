"""Boot a preview: read the app's manifest and turn its ``[preview]`` table into a boot.

Apart from :mod:`update_app_skill.preview_app` because only ``up`` reads a manifest:
``refresh`` and ``down`` work from the preview's recorded state, so they do not load the
manifest library (and the pydantic it builds on) at startup.
"""

from __future__ import annotations

import json
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

from agents_shared import serve_isolated_instance
from app_manifest.manifest import (
    MANIFEST_FILENAME,
    PREVIEW_KEY_PLACEHOLDER,
    AppManifest,
    load_manifest,
)
from app_manifest.registry_location import registry_path
from workspace_bare_scripts import forward_port

from update_app_skill.preview_app import (
    INNER_SERVICE_SUFFIX,
    Instances,
    InstanceSpec,
    PreviewError,
    _preview_state_path,
    _recorded_siblings,
    _reframe_previews_naming,
    _registry_copy_path,
    instance_name,
    live_preview_url,
    live_preview_worktree,
    write_registry_copy,
)

# The placeholder this script fills before the shared substrate sees the table.
REGISTRY_PLACEHOLDER = "{registry}"

# The live app runs from the repo root's venv; a worktree's app runs from the worktree root
# through the worktree's own venv, which is what makes the preview serve the worktree's
# code rather than the live app's. Like a supervisord program, it syncs that environment
# --frozen and then execs the app from it, so no uv process stays resident as its parent.
LAUNCHER = (
    "bash",
    "-c",
    'uv sync --all-packages --frozen && export VIRTUAL_ENV="$PWD/.venv" PATH="$PWD/.venv/bin:$PATH" && exec "$@"',
    "bash",
)

# An app that declares ``[[secrets]]`` runs under the wrapper, which is the only reader
# of its secret files; the preview does the same. The worktree has no ``data/``, so the
# wrapper reads the live repo's files.
WITH_SECRETS_SCRIPT = Path("system") / "scripts" / "with_secrets.py"
SECRETS_DIRECTORY = Path("data") / ".secrets"


def _is_same_worktree(recorded: Path, worktree: Path) -> bool:
    return recorded.resolve() == worktree.resolve()


def find_manifest(worktree: Path, app_name: str) -> tuple[Path, AppManifest]:
    """The manifest under ``worktree/system/apps/*/`` whose name is ``app_name``."""
    apps_dir = worktree / "system" / "apps"
    if not apps_dir.is_dir():
        raise PreviewError(
            f"{apps_dir} is not a directory; is --worktree a workspace checkout?"
        )
    for manifest_path in sorted(apps_dir.glob(f"*/{MANIFEST_FILENAME}")):
        manifest = load_manifest(manifest_path)
        if manifest.name == app_name:
            return manifest_path, manifest
    raise PreviewError(f"no app named {app_name!r} has a manifest under {apps_dir}")


def resolve_own_placeholders(text: str, registry_copy: Path | None) -> str:
    """Fill the placeholders only this script knows; the shared substrate fills the rest."""
    if REGISTRY_PLACEHOLDER in text:
        if registry_copy is None:
            raise PreviewError(
                f"{REGISTRY_PLACEHOLDER} is used, but no registry copy was made"
            )
        text = text.replace(REGISTRY_PLACEHOLDER, str(registry_copy))
    return text


def resolve_open_path(manifest: AppManifest, instance_key: str | None) -> str:
    if manifest.preview.open_path_takes_key:
        if not instance_key:
            raise PreviewError(
                f"a preview of {manifest.name!r} opens on an instance ({manifest.preview.open_path}); "
                "pass --instance-key"
            )
        return manifest.preview.open_path.replace(PREVIEW_KEY_PLACEHOLDER, instance_key)
    return manifest.preview.open_path


def _under_declared_secrets(
    manifest: AppManifest, repo_root: Path, launch: list[str]
) -> list[str]:
    """``launch`` run under ``with_secrets.py`` once per declared secret file, first declared outermost."""
    for secret in reversed(manifest.secrets):
        env_file = repo_root / SECRETS_DIRECTORY / f"{secret.file}.env"
        launch = [
            "python3",
            str(repo_root / WITH_SECRETS_SCRIPT),
            str(env_file),
            "--",
            *launch,
        ]
    return launch


def _declared_theming_mode(worktree: Path, manifest: AppManifest) -> str | None:
    """The ``[theming]`` mode the worktree's own app.toml declares, or None when it declares no ``[theming]``."""
    manifest_path, _ = find_manifest(worktree, str(manifest.name))
    try:
        raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    theming = raw.get("theming")
    if not isinstance(theming, dict):
        return None
    mode = theming.get("mode", "tokens")
    return mode if isinstance(mode, str) else None


def build_instance_spec(
    manifest: AppManifest,
    worktree: Path,
    repo_root: Path,
    title: str,
    registry_copy: Path | None,
    inner_path: str,
) -> InstanceSpec:
    """The shared substrate's boot of the manifest's preview table."""
    preview = manifest.preview
    try:
        port_env_by_name = serve_isolated_instance.parse_port_envs(
            (), [str(port_name) for port_name in preview.ports]
        )
    except serve_isolated_instance.InstanceError as exc:
        raise PreviewError(f"the [preview] table of {manifest.name!r}: {exc}") from exc
    command = list(preview.command) if preview.command else [str(manifest.program)]
    launch = _under_declared_secrets(manifest, repo_root, [*command, *preview.args])
    return InstanceSpec(
        name=instance_name(manifest.name),
        command=[
            *LAUNCHER,
            *(resolve_own_placeholders(part, registry_copy) for part in launch),
        ],
        cwd=worktree,
        port_env_by_name=port_env_by_name,
        env_overrides={
            key: resolve_own_placeholders(value, registry_copy)
            for key, value in preview.env.items()
        },
        copy_sources={str(key): source for key, source in preview.copies.items()},
        health_path=preview.health_path,
        service_name=f"{manifest.name}{INNER_SERVICE_SUFFIX}",
        preview_service_name=instance_name(manifest.name),
        preview_title=title,
        inner_path=inner_path,
        theming_mode=_declared_theming_mode(worktree, manifest),
    )


def up(
    app_name: str,
    worktree: Path,
    repo_root: Path,
    *,
    with_apps: Sequence[str] = (),
    instance_key: str | None = None,
    title: str | None = None,
    instances: Instances,
    dump_registry: Callable[[list[dict[str, object]]], str] | None = None,
) -> int:
    """Boot the app's preview from ``worktree``, after the siblings named in ``with_apps``."""
    other = live_preview_worktree(repo_root, app_name)
    if other is not None and not _is_same_worktree(other, worktree):
        sys.stderr.write(
            f"preview: another pass's preview of {app_name!r} is already up, serving {other}; the "
            f"'{instance_name(app_name)}' app can only be one at a time, so booting this one would "
            "hijack it. Surface this to the user and coordinate with that pass -- or, if it is "
            f"abandoned, tear it down first with 'down --app {app_name}'.\n"
        )
        return 1
    manifest_path, manifest = find_manifest(worktree, app_name)
    dump = dump_registry if dump_registry is not None else forward_port.dump_registry
    # Siblings first, because the registry copy this app is booted with has to name their
    # URLs.
    preview_url_by_app: dict[str, str] = {}
    for sibling in with_apps:
        # A sibling opens on the same instance when its path takes one (the chat under a shell
        # preview opens on the user's conversation, like a chat preview of its own).
        if (
            up(
                sibling,
                worktree,
                repo_root,
                instance_key=instance_key,
                instances=instances,
                dump_registry=dump,
            )
            != 0
        ):
            return 1
        sibling_url = live_preview_url(repo_root, sibling)
        if sibling_url is None:
            sys.stderr.write(
                f"preview: the preview of {sibling!r} came up but recorded no port.\n"
            )
            return 1
        preview_url_by_app[sibling] = sibling_url
    registry_copy: Path | None = None
    if _uses_placeholder(manifest, REGISTRY_PLACEHOLDER):
        registry_copy = _registry_copy_path(repo_root, app_name)
        write_registry_copy(registry_path(), registry_copy, preview_url_by_app, dump)
    spec = build_instance_spec(
        manifest,
        worktree,
        repo_root,
        title if title is not None else f"{manifest.display_name} ({worktree.name})",
        registry_copy,
        resolve_open_path(manifest, instance_key),
    )
    # An earlier ``up --with`` of this preview may have booted siblings this call does not
    # name; they are still running, and ``down`` finds them only through this record.
    siblings = list(
        dict.fromkeys([*with_apps, *_recorded_siblings(repo_root, app_name)])
    )
    _preview_state_path(repo_root, app_name).parent.mkdir(parents=True, exist_ok=True)
    _preview_state_path(repo_root, app_name).write_text(
        json.dumps(
            {
                "app": app_name,
                "worktree": str(worktree),
                "with": siblings,
                "manifest": str(manifest_path),
            }
        )
    )
    code = instances.up(spec, repo_root)
    if code != 0 and not siblings:
        # Kept when siblings may be up: their names live only in this record, so dropping it
        # would leave them running with nothing left for ``down`` to reach them by. They are
        # left running rather than torn down here, since the usual next move is a rebuild and
        # a retry, which reuses them.
        _preview_state_path(repo_root, app_name).unlink(missing_ok=True)
    if code == 0:
        _reframe_previews_naming(repo_root, app_name, dump)
    return code


def _uses_placeholder(manifest: AppManifest, placeholder: str) -> bool:
    preview = manifest.preview
    texts = [*preview.command, *preview.args, *preview.env.values()]
    return any(placeholder in text for text in texts)
