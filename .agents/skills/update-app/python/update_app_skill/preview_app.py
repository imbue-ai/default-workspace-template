"""Boot, refresh, and tear down a preview of any app from its manifest's ``[preview]`` table.

A preview is a throwaway instance of an app built in an editing worktree, served on
free ports beside the live one and surfaced to the user as a labeled ``<name>-preview``
app whose window frames it. What it takes to boot one is the app's own business,
declared in its ``app.toml`` (``PreviewSpec`` in ``system/libs/app_manifest``): the
command, the named ports, the environment, the directories copied into its scratch
space, the health path, and the path the window opens on. This script reads that
table from the worktree, fills in the one placeholder only it knows -- ``{registry}``,
a copy of the live registry whose rows for previewed sibling apps point at their
previews -- and hands the rest to the shared ``serve_isolated_instance``, which
owns the ports, the copies, the processes, the registrations, and the state file.

One preview per app at a time: a preview of the same app from a different worktree
(another pass) is refused rather than hijacked. ``--with`` boots sibling previews
from the same worktree first, so a shell preview can frame them.

Run from the repo root via ``uv run --no-sync`` (it imports the manifest library from
the root venv):

    uv run --no-sync .agents/skills/update-app/python/update_app_skill/preview_app.py up \\
        --app <name> --worktree <dir> [--with <name>]... [--instance-key <key>] \\
        [--title <label>] [--repo-root PATH]
    uv run --no-sync .agents/skills/update-app/python/update_app_skill/preview_app.py refresh --app <name> [--repo-root PATH]
    uv run --no-sync .agents/skills/update-app/python/update_app_skill/preview_app.py down --app <name> [--repo-root PATH]

``up`` prints the preview's app name (``<name>-preview``) on stdout; open a window
of it with ``uv run --no-sync workspace-layout open <name>-preview``. ``refresh``
re-boots the inner process in place after a rebuild, leaving the ports, the wrapper,
the registrations, and the window untouched. ``down`` tears the preview down together
with the siblings it booted, and verifies the processes died.

Exit codes:
    0  Success.
    1  The manifest could not be read, the table could not be resolved, another
       pass's preview is up, or the shared boot failed (which quotes the boot log).
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agents_shared import serve_isolated_instance
from app_manifest.errors import ManifestLoadError
from app_manifest.registry_location import registry_path
from workspace_bare_scripts import forward_port

# Where the shared substrate files each instance, and where this script keeps what it
# adds: the registry copy and the sibling list.
INSTANCES_ROOT = serve_isolated_instance.STATE_ROOT
INSTANCE_STATE_FILENAME = serve_isolated_instance.STATE_FILENAME
PREVIEW_STATE_SUFFIX = ".preview.json"
REGISTRY_COPY_SUFFIX = ".registry.toml"

# The registrations a preview makes: the inner app at its own origin (internal, since
# the frame is how it is meant to be reached), and the labeled wrapper frame the user
# opens, which is the one row a preview puts in the launcher.
INNER_SERVICE_SUFFIX = "-preview-app"
PREVIEW_SERVICE_SUFFIX = "-preview"

# The shell's app name: the one preview whose registry copy other previews are framed through.
SHELL_APP_NAME = "system_interface"


class PreviewError(Exception):
    """A preview could not be described or booted."""


@dataclass(frozen=True)
class InstanceSpec:
    """One preview's boot, as ``serve_isolated_instance.up`` takes it."""

    name: str
    command: list[str]
    cwd: Path
    port_env_by_name: dict[str, str | None]
    env_overrides: dict[str, str]
    copy_sources: dict[str, str]
    health_path: str
    service_name: str
    preview_service_name: str
    preview_title: str
    inner_path: str
    theming_mode: str | None


class Instances:
    """Indirection over the shared isolated-instance substrate, so tests can stand in for it."""

    def up(self, spec: InstanceSpec, repo_root: Path) -> int:
        return serve_isolated_instance.up(
            spec.name,
            spec.command,
            str(spec.cwd),
            repo_root,
            port_env_by_name=spec.port_env_by_name,
            env_overrides=spec.env_overrides,
            copy_sources=spec.copy_sources,
            health_path=spec.health_path,
            service_name=spec.service_name,
            preview_service_name=spec.preview_service_name,
            preview_title=spec.preview_title,
            inner_path=spec.inner_path,
            theming_mode=spec.theming_mode,
            runner=serve_isolated_instance.Runner(),
            registry=serve_isolated_instance.Registry(),
            http=serve_isolated_instance.HttpClient(),
            spawner=serve_isolated_instance.Spawner(),
        )

    def refresh(self, name: str, repo_root: Path) -> int:
        return serve_isolated_instance.refresh(
            name,
            repo_root,
            runner=serve_isolated_instance.Runner(),
            http=serve_isolated_instance.HttpClient(),
            spawner=serve_isolated_instance.Spawner(),
        )

    def down(self, name: str, repo_root: Path) -> int:
        return serve_isolated_instance.down(
            name,
            repo_root,
            runner=serve_isolated_instance.Runner(),
            registry=serve_isolated_instance.Registry(),
        )


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


def write_registry_copy(
    live_registry: Path,
    destination: Path,
    preview_url_by_app: Mapping[str, str],
    dump_registry: Callable[[list[dict[str, object]]], str],
) -> None:
    """Copy the live registry with each previewed sibling's row pointing at its preview.

    A row's ``url`` is where a shell reaches the app over loopback;
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
        preview_label = label_by_name.get(f"{name}{INNER_SERVICE_SUFFIX}")
        if preview_label is None:
            raise PreviewError(
                f"the preview of {name!r} is not registered as {name}{INNER_SERVICE_SUFFIX}, so a shell "
                "preview cannot frame it"
            )
        app["label"] = preview_label
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(dump_registry(apps))


def _reframe_previews_naming(
    repo_root: Path,
    sibling: str,
    dump_registry: Callable[[list[dict[str, object]]], str],
) -> None:
    """Rewrite the registry copy of every live preview that frames ``sibling``.

    A framing preview (the shell's) reads its siblings' URLs from a registry copy
    written when it booted, and it watches that file. A sibling re-upped on its own
    moves to new ports, and one taken down on its own is gone; either way the copy
    is rewritten with where every recorded sibling is now, so the framing preview
    follows without a re-up of its own.
    """
    for state_path in sorted(
        (repo_root / INSTANCES_ROOT).glob(f"*{PREVIEW_STATE_SUFFIX}")
    ):
        state = _read_json(state_path)
        if state is None:
            continue
        framing = str(state.get("app") or "")
        recorded = state.get("with")
        if (
            framing == sibling
            or not isinstance(recorded, list)
            or sibling not in recorded
            or live_preview_url(repo_root, framing) is None
        ):
            continue
        registry_copy = _registry_copy_path(repo_root, framing)
        if not registry_copy.exists():
            continue
        preview_url_by_app = {
            str(name): url
            for name in recorded
            if (url := live_preview_url(repo_root, str(name))) is not None
        }
        write_registry_copy(
            registry_path(), registry_copy, preview_url_by_app, dump_registry
        )


def refresh(app_name: str, repo_root: Path, *, instances: Instances) -> int:
    """Re-boot the preview's inner process in place, after a rebuild in its worktree."""
    return instances.refresh(instance_name(app_name), repo_root)


def _recorded_siblings(repo_root: Path, app_name: str) -> list[str]:
    """The siblings the app's preview record says were booted with it."""
    state = _read_json(_preview_state_path(repo_root, app_name))
    recorded = state.get("with") if state is not None else None
    if not isinstance(recorded, list):
        return []
    return [str(name) for name in recorded]


def down(
    app_name: str,
    repo_root: Path,
    *,
    instances: Instances,
    dump_registry: Callable[[list[dict[str, object]]], str] | None = None,
) -> int:
    """Tear the preview down, then the siblings it booted; keep everything a survivor still needs.

    A preview that another live preview frames (a chat taken down on its own under a
    shell preview) leaves that preview's registry copy pointing at the live app again.
    """
    siblings = _recorded_siblings(repo_root, app_name)
    dump = dump_registry if dump_registry is not None else forward_port.dump_registry
    code = instances.down(instance_name(app_name), repo_root)
    if code != 0:
        return code
    _registry_copy_path(repo_root, app_name).unlink(missing_ok=True)
    _preview_state_path(repo_root, app_name).unlink(missing_ok=True)
    _reframe_previews_naming(repo_root, app_name, dump)
    for sibling in siblings:
        code = down(sibling, repo_root, instances=instances, dump_registry=dump)
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
        "up",
        help="Boot the app's preview from a worktree and surface it as an app to open.",
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
        help="The instance the window opens on, for an app whose open_path takes one.",
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
            from update_app_skill.preview_boot import up

            return up(
                args.app,
                Path(args.worktree).resolve(),
                repo_root,
                with_apps=args.with_apps,
                instance_key=args.instance_key,
                title=args.title,
                instances=Instances(),
            )
        if args.command == "refresh":
            return refresh(args.app, repo_root, instances=Instances())
        if args.command == "down":
            return down(args.app, repo_root, instances=Instances())
    except (PreviewError, ManifestLoadError) as exc:
        sys.stderr.write(f"preview: {exc}\n")
        return 1
    parser.error(f"unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
