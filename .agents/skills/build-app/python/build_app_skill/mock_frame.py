"""Show a new app framed as a mock until it is really hooked up, then take the frame down.

While an app built through ``build-app`` is still a mock -- hard-coded content, or a
sample of real data that is not yet wired to its source, with some of it not working
-- the user sees it only inside the labeled preview frame, with a "Mock" banner and a
note saying what is not real yet. ``up`` frames the app's live program as the
``<name>-mock`` app (nothing else is booted: the frame shows the app the agent is
editing), and re-registers the app itself from its manifest as internal and never
stopped for want of a window. Internal keeps the bare app out of the launcher (a
desktop shortcut the shell gave it when it was first registered stays); and since only
the frame has a window, the shell would otherwise stop the app a minute after its own
window count reached zero. Both last until the app's next
registration from its manifest alone, which its program makes on every start, so ``up``
is re-run after every restart (``verify_app.py --restart`` and then ``up --refresh``,
in one call). ``down`` closes the mock window, removes the frame, and registers the app
from its manifest again, so it is an ordinary app the user can open; for an app that has
since been removed it takes the frame down and leaves the registry alone.

Run from the repo root via ``uv run --no-sync``:

    uv run --no-sync .agents/skills/build-app/python/build_app_skill/mock_frame.py up \\
        --app <name> --note "<what is not real yet>" [--path /page] [--open | --refresh]
    uv run --no-sync .agents/skills/build-app/python/build_app_skill/mock_frame.py down --app <name>

Exit codes:
    0  Success.
    1  ``up``: the app has no manifest or is not registered, the frame did not come up,
       or the window could not be opened or refreshed. ``down``: the frame could not be
       torn down.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agents_shared import preview_wrapper_server, serve_isolated_instance
from app_manifest.errors import ManifestLoadError, RegistryReadError
from app_manifest.manifest import load_manifest
from app_manifest.registry import read_registry
from workspace_bare_scripts import forward_port
from workspace_layout.cli import context_from_environment, run_layout_cli

FRAME_SUFFIX = "-mock"
DISPLAY_NAME_SUFFIX = " (mock)"
# What the app's own registry row says while the frame is up: not an app to open (the
# frame is), and not stopped when no window shows it (the frame's window does).
FRAMED_APP_OVERRIDES: dict[str, object] = {
    "internal": True,
    "stop_when_no_windows": False,
}


class MockFrameError(Exception):
    """The app cannot be framed: it has no manifest, or it is not registered."""


@dataclass(frozen=True)
class FramedApp:
    """The live app a frame shows: its name, its manifest, what users call it, and where it runs."""

    name: str
    manifest_path: Path
    display_name: str
    url: str


class Frames:
    """Indirection over the shared frame substrate, so tests can stand in for it."""

    def up(self, app: FramedApp, note: str, inner_path: str, repo_root: Path) -> int:
        return serve_isolated_instance.frame(
            frame_name(app.name),
            repo_root,
            inner_service=app.name,
            frame_service_name=frame_name(app.name),
            title=app.display_name,
            display_name=frame_display_name(app.display_name),
            kind=preview_wrapper_server.KIND_MOCK,
            note=note,
            inner_path=inner_path,
            runner=serve_isolated_instance.Runner(),
            registry=serve_isolated_instance.Registry(),
            http=serve_isolated_instance.HttpClient(),
            spawner=serve_isolated_instance.Spawner(),
        )

    def down(self, app_name: str, repo_root: Path) -> int:
        return serve_isolated_instance.down(
            frame_name(app_name),
            repo_root,
            runner=serve_isolated_instance.Runner(),
            registry=serve_isolated_instance.Registry(),
        )


class Layout:
    """Indirection over ``workspace-layout``, so tests can record what would change on screen."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def run(self, argv: Sequence[str]) -> int:
        """Run one ``workspace-layout`` op; its exit code (the command exits on a refusal)."""
        context = context_from_environment()
        try:
            return run_layout_cli(
                list(argv),
                context._replace(apps_file=self.repo_root / context.apps_file),
            )
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1


def frame_name(app_name: str) -> str:
    return f"{app_name}{FRAME_SUFFIX}"


def frame_display_name(display_name: str) -> str:
    """What the frame's window is called: the app's own name, marked as a mock, within the registry's cap."""
    room = forward_port.MAX_DISPLAY_NAME_LENGTH - len(DISPLAY_NAME_SUFFIX)
    return f"{display_name[:room].rstrip()}{DISPLAY_NAME_SUFFIX}"


def find_app(app_name: str, repo_root: Path) -> FramedApp:
    """The registered app scaffolded as ``app_name``; raises MockFrameError when there is none."""
    manifest_path = (
        repo_root / "system" / "apps" / app_name.replace("-", "_") / "app.toml"
    )
    try:
        manifest = load_manifest(manifest_path, repo_root=repo_root)
    except ManifestLoadError as exc:
        raise MockFrameError(f"no app named {app_name!r} to frame: {exc}") from exc
    try:
        rows = read_registry(forward_port.apps_file(repo_root))
    except RegistryReadError as exc:
        raise MockFrameError(str(exc)) from exc
    row = next((row for row in rows if row.name == app_name), None)
    if row is None:
        raise MockFrameError(
            f"{app_name!r} is not registered, so there is nothing running to frame; start "
            "it first (the scaffold's --start, or supervisorctl start)"
        )
    return FramedApp(
        name=app_name,
        manifest_path=manifest_path,
        display_name=str(manifest.display_name),
        url=str(row.url),
    )


def _register(
    app: FramedApp, repo_root: Path, overrides: dict[str, object] | None
) -> None:
    try:
        forward_port.register_manifest(
            forward_port.apps_file(repo_root),
            app.manifest_path,
            app.url,
            overrides=overrides,
        )
    except forward_port.RegistryError as exc:
        raise MockFrameError(f"could not re-register {app.name!r}: {exc}") from exc


def up(
    app_name: str,
    note: str,
    repo_root: Path,
    *,
    inner_path: str = "/",
    surface: str | None = None,
    frames: Frames,
    layout: Layout,
) -> int:
    """Frame the app as a mock with ``note``, then open (``surface="open"``) or reload (``"refresh"``) its window."""
    if not note.strip():
        sys.stderr.write("mock frame: --note must say what is not real yet.\n")
        return 1
    app = find_app(app_name, repo_root)
    _register(app, repo_root, FRAMED_APP_OVERRIDES)
    if frames.up(app, note, inner_path, repo_root) != 0:
        _register(app, repo_root, None)
        return 1
    if surface is None:
        sys.stderr.write(
            f"mock frame up: open it with `uv run --no-sync workspace-layout open {frame_name(app_name)} --beside`.\n"
        )
        return 0
    argv = (
        ["open", frame_name(app_name), "--beside"]
        if surface == "open"
        else ["refresh", "--app", frame_name(app_name)]
    )
    if layout.run(argv) != 0:
        sys.stderr.write(
            f"mock frame: workspace-layout {' '.join(argv)} failed (see above).\n"
        )
        return 1
    return 0


def down(app_name: str, repo_root: Path, *, frames: Frames, layout: Layout) -> int:
    """Close the mock window, remove the frame, and make the app an ordinary app again.

    The frame comes down even when the app itself is gone (removed while still a mock);
    its registration is then left alone, since registering it would add it back.
    """
    if layout.run(["close", frame_name(app_name)]) != 0:
        sys.stderr.write(f"note: no {frame_name(app_name)} window was open to close.\n")
    code = frames.down(app_name, repo_root)
    try:
        app = find_app(app_name, repo_root)
    except MockFrameError:
        sys.stderr.write(
            f"mock frame: {app_name!r} has no manifest or is not registered, so its "
            "registration is left as it is.\n"
        )
        return code
    _register(app, repo_root, None)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show a new app framed as a mock, or take the frame down once the app is real."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    up_parser = subparsers.add_parser(
        "up", help="Frame the app's live program as a mock (re-run after each restart)."
    )
    up_parser.add_argument("--app", required=True, help="The app's registered name.")
    up_parser.add_argument(
        "--note",
        required=True,
        help="One plain sentence for the user saying what is not real yet, e.g. "
        "'Showing 20 of your emails as a sample; refresh does not work yet.'",
    )
    up_parser.add_argument(
        "--path", default="/", help="The page the frame opens the app on (default: /)."
    )
    surface = up_parser.add_mutually_exclusive_group()
    surface.add_argument(
        "--open", action="store_true", help="Open the mock window beside the chat."
    )
    surface.add_argument(
        "--refresh", action="store_true", help="Reload the open mock window."
    )

    down_parser = subparsers.add_parser(
        "down",
        help="Close the mock window and remove the frame, leaving the app an ordinary one.",
    )
    down_parser.add_argument("--app", required=True, help="The app's registered name.")

    for subparser in (up_parser, down_parser):
        subparser.add_argument(
            "--repo-root",
            default=".",
            help="Path to the repository root (default: current directory).",
        )

    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    layout = Layout(repo_root)
    try:
        if args.command == "up":
            return up(
                args.app,
                args.note,
                repo_root,
                inner_path=args.path,
                surface="open" if args.open else "refresh" if args.refresh else None,
                frames=Frames(),
                layout=layout,
            )
        return down(args.app, repo_root, frames=Frames(), layout=layout)
    except MockFrameError as exc:
        sys.stderr.write(f"mock frame: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
