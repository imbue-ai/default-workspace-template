#!/usr/bin/env python3
"""Boot, refresh, and tear down a preview of any app from its manifest's ``[preview]`` table.

A preview is a throwaway instance of an app built in an editing worktree, served on
free ports beside the live one and surfaced to the user as a labeled ``<name>-preview``
tab. What it takes to boot one is the app's own business, declared in its
``app.toml`` (``PreviewSpec`` in ``system/libs/app_manifest``): the command, the
named ports, the environment, the directories copied into its scratch space, the
health path, and the path the tab opens on. This script reads that table from the
worktree, fills in the two placeholders only it knows -- ``{registry}`` (a copy of
the live registry whose rows for previewed sibling apps point at their previews)
and ``{shell_url}`` (the preview shell's URL when one is up) -- and hands the rest
to the shared ``serve_isolated_instance.py``, which owns the ports, the copies, the
processes, the registrations, and the state file.

One preview per app at a time: a preview of the same app from a different worktree
(another pass) is refused rather than hijacked. ``--with`` boots sibling previews
from the same worktree first, so a shell preview can frame them.

Run from the repo root via ``uv run python3`` (it imports the manifest library from
the root venv):

    uv run python3 .agents/skills/update-app/scripts/preview_app.py up \\
        --app <name> --worktree <dir> [--with <name>]... [--instance-key <key>] \\
        [--title <label>] [--repo-root PATH]
    uv run python3 .agents/skills/update-app/scripts/preview_app.py refresh --app <name> [--repo-root PATH]
    uv run python3 .agents/skills/update-app/scripts/preview_app.py down --app <name> [--repo-root PATH]

``up`` prints the preview's tab name (``<name>-preview``) on stdout; open it with
``python3 system/scripts/layout.py open <name>-preview``. ``refresh`` re-boots the
inner process in place after a rebuild, leaving the ports, the wrapper, the
registrations, and the tab untouched. ``down`` tears the preview down together with
the siblings it booted, and verifies the processes died.

Exit codes:
    0  Success.
    1  The manifest could not be read, the table could not be resolved, another
       pass's preview is up, or the shared script failed (which quotes the boot log).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from app_manifest.errors import ManifestLoadError
from app_manifest.manifest import (
    MANIFEST_FILENAME,
    PREVIEW_KEY_PLACEHOLDER,
    AppManifest,
    load_manifest,
)
from app_manifest.registry import registry_path

# Where the shared script files each instance (its STATE_ROOT / STATE_FILENAME), and
# where this script keeps what it adds: the registry copy and the sibling list.
INSTANCES_ROOT = "data/.state/isolated-instances"
INSTANCE_STATE_FILENAME = "instance.json"
PREVIEW_STATE_SUFFIX = ".preview.json"
REGISTRY_COPY_SUFFIX = ".registry.toml"

# The registrations a preview makes: the inner app at its own origin, and the labeled
# wrapper frame the user opens.
INNER_SERVICE_SUFFIX = "-preview-app"
PREVIEW_SERVICE_SUFFIX = "-preview"

# The placeholders this script fills before the shared script sees the table.
REGISTRY_PLACEHOLDER = "{registry}"
SHELL_URL_PLACEHOLDER = "{shell_url}"

# The shell's app name: the one preview whose registry copy other previews are framed through.
SHELL_APP_NAME = "system_interface"

# Every app's tool runs from the repo root; a worktree's app runs from the worktree root
# through its own environment, which is what makes the preview serve the worktree's code
# rather than the live tool's.
LAUNCHER = ("uv", "run")

_SHARED_SERVE_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "shared"
    / "scripts"
    / "serve_isolated_instance.py"
)
_FORWARD_PORT_SCRIPT = (
    Path(__file__).resolve().parents[4] / "system" / "scripts" / "forward_port.py"
)


class PreviewError(Exception):
    """A preview could not be described or booted."""


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept the shared script."""

    def run(self, argv: Sequence[str], cwd: Path) -> int:
        return int(subprocess.run(list(argv), cwd=str(cwd), check=False).returncode)


def _load_forward_port_module():
    """The registration script, for its registry writer: stdlib-only, so it imports from its path."""
    spec = importlib.util.spec_from_file_location("forward_port", _FORWARD_PORT_SCRIPT)
    if spec is None or spec.loader is None:
        raise PreviewError(
            f"cannot load the registration script at {_FORWARD_PORT_SCRIPT}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def instance_name(app_name: str) -> str:
    return f"{app_name}{PREVIEW_SERVICE_SUFFIX}"


def _instance_state_path(repo_root: Path, app_name: str) -> Path:
    return (
        repo_root / INSTANCES_ROOT / instance_name(app_name) / INSTANCE_STATE_FILENAME
    )


def _preview_state_path(repo_root: Path, app_name: str) -> Path:
    return (
        repo_root / INSTANCES_ROOT / f"{instance_name(app_name)}{PREVIEW_STATE_SUFFIX}"
    )


def _registry_copy_path(repo_root: Path, app_name: str) -> Path:
    return (
        repo_root / INSTANCES_ROOT / f"{instance_name(app_name)}{REGISTRY_COPY_SUFFIX}"
    )


def _read_json(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PreviewError(f"could not read {path}: {exc}") from exc
    return loaded if isinstance(loaded, dict) else None


def live_preview_worktree(repo_root: Path, app_name: str) -> Path | None:
    """The worktree the app's current preview serves, or None when none is up."""
    state = _read_json(_instance_state_path(repo_root, app_name))
    if state is None:
        return None
    cwd = state.get("cwd")
    return Path(str(cwd)) if cwd else None


def live_preview_url(repo_root: Path, app_name: str) -> str | None:
    """The loopback URL of the app's current preview, or None when none is up."""
    state = _read_json(_instance_state_path(repo_root, app_name))
    if state is None:
        return None
    port = state.get("inner_port")
    return f"http://127.0.0.1:{int(str(port))}" if port else None


def _is_same_worktree(recorded: Path, worktree: Path) -> bool:
    return recorded.resolve() == worktree.resolve()


def write_registry_copy(
    live_registry: Path,
    destination: Path,
    preview_url_by_app: Mapping[str, str],
    dump_registry: Callable[[list[dict[str, object]]], str],
) -> None:
    """Copy the live registry with each previewed sibling's row pointing at its preview.

    A row's ``url`` and ``instances_url`` are where a shell reaches the app over loopback;
    its ``label`` is the origin the browser frames it at, so the sibling's row takes the
    label the sibling's own ``<name>-preview-app`` registration was given. Every other
    row is the live one: a preview shell shows the real workspace with one app swapped.
    """
    apps: list[dict[str, object]] = []
    label_by_name: dict[str, str] = {}
    if live_registry.exists():
        with open(live_registry, "rb") as handle:
            loaded = tomllib.load(handle).get("apps", [])
        apps = [dict(app) for app in loaded if isinstance(app, dict)]
    for app in apps:
        label = app.get("label")
        if isinstance(app.get("name"), str) and isinstance(label, str):
            label_by_name[str(app["name"])] = label
    for app in apps:
        name = app.get("name")
        if not isinstance(name, str) or name not in preview_url_by_app:
            continue
        preview_url = preview_url_by_app[name]
        app["url"] = preview_url
        if "instances_url" in app:
            app["instances_url"] = preview_url
        preview_label = label_by_name.get(f"{name}{INNER_SERVICE_SUFFIX}")
        if preview_label is None:
            raise PreviewError(
                f"the preview of {name!r} is not registered as {name}{INNER_SERVICE_SUFFIX}, so a shell "
                "preview cannot frame it"
            )
        app["label"] = preview_label
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(dump_registry(apps))


def resolve_own_placeholders(
    text: str, registry_copy: Path | None, shell_url: str
) -> str:
    """Fill the placeholders only this script knows; the shared script fills the rest."""
    if REGISTRY_PLACEHOLDER in text:
        if registry_copy is None:
            raise PreviewError(
                f"{REGISTRY_PLACEHOLDER} is used, but no registry copy was made"
            )
        text = text.replace(REGISTRY_PLACEHOLDER, str(registry_copy))
    return text.replace(SHELL_URL_PLACEHOLDER, shell_url)


def resolve_open_path(manifest: AppManifest, instance_key: str | None) -> str:
    if manifest.preview.open_path_takes_key:
        if not instance_key:
            raise PreviewError(
                f"a preview of {manifest.name!r} opens on an instance ({manifest.preview.open_path}); "
                "pass --instance-key"
            )
        return manifest.preview.open_path.replace(PREVIEW_KEY_PLACEHOLDER, instance_key)
    return manifest.preview.open_path


def build_up_argv(
    manifest: AppManifest,
    worktree: Path,
    repo_root: Path,
    title: str,
    registry_copy: Path | None,
    shell_url: str,
    inner_path: str,
) -> list[str]:
    """The shared script's ``up`` invocation for the manifest's preview table."""
    preview = manifest.preview
    argv = [
        sys.executable,
        str(_SHARED_SERVE_SCRIPT),
        "up",
        "--name",
        instance_name(manifest.name),
        "--cwd",
        str(worktree),
        "--repo-root",
        str(repo_root),
        "--health-path",
        preview.health_path,
        "--service-name",
        f"{manifest.name}{INNER_SERVICE_SUFFIX}",
        "--preview-service-name",
        instance_name(manifest.name),
        "--preview-title",
        title,
        "--inner-path",
        inner_path,
    ]
    for port_name in preview.ports:
        argv.extend(["--port", str(port_name)])
    for key, value in preview.env.items():
        argv.extend(
            [
                "--env",
                f"{key}={resolve_own_placeholders(value, registry_copy, shell_url)}",
            ]
        )
    for key, source in preview.copies.items():
        argv.extend(["--copy", f"{key}={source}"])
    command = list(preview.command) if preview.command else [str(manifest.program)]
    launch = [*command, *preview.args]
    argv.append("--")
    argv.extend(
        [
            *LAUNCHER,
            *(
                resolve_own_placeholders(part, registry_copy, shell_url)
                for part in launch
            ),
        ]
    )
    return argv


def up(
    app_name: str,
    worktree: Path,
    repo_root: Path,
    *,
    with_apps: Sequence[str] = (),
    instance_key: str | None = None,
    title: str | None = None,
    runner: Runner,
    dump_registry: Callable[[list[dict[str, object]]], str] | None = None,
) -> int:
    """Boot the app's preview from ``worktree``, after the siblings named in ``with_apps``."""
    other = live_preview_worktree(repo_root, app_name)
    if other is not None and not _is_same_worktree(other, worktree):
        sys.stderr.write(
            f"preview: another pass's preview of {app_name!r} is already up, serving {other}; the "
            f"'{instance_name(app_name)}' tab can only show one at a time, so booting this one would "
            "hijack it. Surface this to the user and coordinate with that pass -- or, if it is "
            f"abandoned, tear it down first with 'down --app {app_name}'.\n"
        )
        return 1
    manifest_path, manifest = find_manifest(worktree, app_name)
    # Siblings first, because the registry copy this app is booted with has to name their
    # URLs. A sibling therefore boots before this app exists: a chat previewed under a shell
    # resolves {shell_url} to "" and runs without a nudger, so the preview shell refetches
    # its instance list on its own sweep rather than on the chat's word.
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
                runner=runner,
                dump_registry=dump_registry,
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
        write_registry_copy(
            registry_path(),
            registry_copy,
            preview_url_by_app,
            dump_registry
            if dump_registry is not None
            else _load_forward_port_module().dump_registry,
        )
    shell_url = (
        ""
        if app_name == SHELL_APP_NAME
        else (live_preview_url(repo_root, SHELL_APP_NAME) or "")
    )
    argv = build_up_argv(
        manifest,
        worktree,
        repo_root,
        title if title is not None else f"{manifest.display_name} ({worktree.name})",
        registry_copy,
        shell_url,
        resolve_open_path(manifest, instance_key),
    )
    _preview_state_path(repo_root, app_name).parent.mkdir(parents=True, exist_ok=True)
    _preview_state_path(repo_root, app_name).write_text(
        json.dumps(
            {
                "app": app_name,
                "worktree": str(worktree),
                "with": list(with_apps),
                "manifest": str(manifest_path),
            }
        )
    )
    code = runner.run(argv, cwd=repo_root)
    if code != 0:
        _preview_state_path(repo_root, app_name).unlink(missing_ok=True)
    return code


def _uses_placeholder(manifest: AppManifest, placeholder: str) -> bool:
    preview = manifest.preview
    texts = [*preview.command, *preview.args, *preview.env.values()]
    return any(placeholder in text for text in texts)


def refresh(app_name: str, repo_root: Path, *, runner: Runner) -> int:
    """Re-boot the preview's inner process in place, after a rebuild in its worktree."""
    return runner.run(
        [
            sys.executable,
            str(_SHARED_SERVE_SCRIPT),
            "refresh",
            "--name",
            instance_name(app_name),
            "--repo-root",
            str(repo_root),
        ],
        cwd=repo_root,
    )


def down(app_name: str, repo_root: Path, *, runner: Runner) -> int:
    """Tear the preview down, then the siblings it booted; keep everything a survivor still needs."""
    state = _read_json(_preview_state_path(repo_root, app_name))
    siblings = (
        [str(name) for name in state.get("with", [])]
        if state is not None and isinstance(state.get("with"), list)
        else []
    )
    code = runner.run(
        [
            sys.executable,
            str(_SHARED_SERVE_SCRIPT),
            "down",
            "--name",
            instance_name(app_name),
            "--repo-root",
            str(repo_root),
        ],
        cwd=repo_root,
    )
    if code != 0:
        return code
    _registry_copy_path(repo_root, app_name).unlink(missing_ok=True)
    _preview_state_path(repo_root, app_name).unlink(missing_ok=True)
    for sibling in siblings:
        code = down(sibling, repo_root, runner=runner)
        if code != 0:
            return code
    return 0


def _add_repo_root_arg(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the live repository root (default: current directory).",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Boot, refresh, or tear down a preview of an app from its manifest's [preview] table."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    up_parser = subparsers.add_parser(
        "up", help="Boot the app's preview from a worktree and surface it as a tab."
    )
    up_parser.add_argument(
        "--app", required=True, help="The app's registered name (its manifest's name)."
    )
    up_parser.add_argument(
        "--worktree", required=True, help="The editing worktree holding the built app."
    )
    up_parser.add_argument(
        "--with",
        dest="with_apps",
        action="append",
        default=[],
        metavar="NAME",
        help="A sibling app to preview from the same worktree first (repeatable); a shell preview frames it.",
    )
    up_parser.add_argument(
        "--instance-key",
        default=None,
        help="The instance the tab opens on, for an app whose open_path takes one.",
    )
    up_parser.add_argument(
        "--title",
        default=None,
        help="The label shown in the preview frame (default: the app and the worktree).",
    )
    _add_repo_root_arg(up_parser)

    refresh_parser = subparsers.add_parser(
        "refresh", help="Re-boot the preview's inner process in place after a rebuild."
    )
    refresh_parser.add_argument(
        "--app", required=True, help="The app's registered name."
    )
    _add_repo_root_arg(refresh_parser)

    down_parser = subparsers.add_parser(
        "down", help="Tear the preview down, with the siblings it booted. Idempotent."
    )
    down_parser.add_argument("--app", required=True, help="The app's registered name.")
    _add_repo_root_arg(down_parser)

    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    try:
        if args.command == "up":
            return up(
                args.app,
                Path(args.worktree).resolve(),
                repo_root,
                with_apps=args.with_apps,
                instance_key=args.instance_key,
                title=args.title,
                runner=Runner(),
            )
        if args.command == "refresh":
            return refresh(args.app, repo_root, runner=Runner())
        if args.command == "down":
            return down(args.app, repo_root, runner=Runner())
    except (PreviewError, ManifestLoadError) as exc:
        sys.stderr.write(f"preview: {exc}\n")
        return 1
    parser.error(f"unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
