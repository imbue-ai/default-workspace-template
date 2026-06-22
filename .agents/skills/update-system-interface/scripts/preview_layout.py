#!/usr/bin/env python3
"""Drive ``scripts/layout.py`` against a running pre-merge preview instance.

The ``update-system-interface`` ``preview`` step boots the worker's built
work_dir as a second system_interface instance on a free port and records that
port in the preview state file. This wrapper reads that port and runs the normal
``manage-layout`` helper (``scripts/layout.py``) against the *preview* instead of
the live UI, by pointing ``MINDS_WORKSPACE_SERVER_URL`` at the preview's inner
port. Every ``layout.py`` subcommand works unchanged -- ``inspect``, ``open``,
``focus``, ``split``, ``maximize``, etc. -- but the ops land on the preview's
dockview (the iframe inside the labeled "preview" tab), not the live one.

This is what lets the previewing agent navigate the preview straight to the
scenario being reviewed (open the relevant chat, arrange the panels, maximize
the progress view) so the user doesn't have to click around to find the change.

Because ``preview`` redirects the preview's layout persistence to a throwaway
dir (rather than neutering it), the preview supports a full ``inspect`` -- so
``inspect`` / ``list`` reflect the real preview state and wait-stable
confirmation + no-op detection work as against the live UI. Anchor splits/moves
with explicit refs (e.g. ``chat:<name>``, which ``list`` surfaces); the ``self``
ref only resolves when that agent's own chat tab is already open in the preview,
which isn't guaranteed there.

Run via bare ``python3`` (standard library only, no venv needed):

    python3 .agents/skills/update-system-interface/scripts/preview_layout.py \\
        --slug <name> inspect
    python3 .agents/skills/update-system-interface/scripts/preview_layout.py \\
        --slug <name> open chat:alice
    python3 .agents/skills/update-system-interface/scripts/preview_layout.py \\
        --slug <name> maximize chat:alice

The layout subcommand and its arguments follow ``--slug`` (and the optional
``--repo-root``). Exit codes are passed through from ``scripts/layout.py``
(0 ok, 1 error, 3 mutex conflict); this wrapper adds exit code 1 for "no active
preview for that slug / malformed preview state".
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

# Runs the resolved layout.py argv and returns its exit code. Injected into
# ``run`` (default: a real subprocess) so tests can observe the hand-off without
# launching a server -- mirroring the Runner/Spawner injection in
# ``reveal_system_interface.py``.
LayoutRunner = Callable[[list[str], str, dict[str, str]], int]

# The agent-facing layout helper this wrapper delegates to, and the env var it
# reads to choose which workspace server to talk to.
LAYOUT_SCRIPT_RELPATH = "scripts/layout.py"
ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"

# Preview state location -- must match ``reveal_system_interface.py``'s
# PREVIEW_STATE_ROOT / PREVIEW_STATE_FILENAME (the producer of this file).
PREVIEW_STATE_ROOT = "runtime/system-interface-preview"
PREVIEW_STATE_FILENAME = "preview.json"


class PreviewLayoutError(Exception):
    """No usable preview state for the requested slug."""


def repo_root_default() -> Path:
    """Repo root inferred from this script's location.

    This file lives at
    ``.agents/skills/update-system-interface/scripts/preview_layout.py``; the
    repo root is four parents up. Used as the ``--repo-root`` default so the
    wrapper works regardless of the caller's cwd.
    """
    return Path(__file__).resolve().parents[4]


def preview_state_path(repo_root: Path, slug: str) -> Path:
    return repo_root / PREVIEW_STATE_ROOT / slug / PREVIEW_STATE_FILENAME


def load_preview_state(state_path: Path) -> dict[str, Any]:
    """Read and JSON-decode the preview state file, or raise PreviewLayoutError."""
    if not state_path.exists():
        raise PreviewLayoutError(
            f"no active preview state at {state_path}; run "
            f"'reveal_system_interface.py preview --slug <name> ...' first"
        )
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PreviewLayoutError(f"could not read preview state {state_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PreviewLayoutError(f"preview state {state_path} is not a JSON object")
    return data


def preview_base_url(state: dict[str, Any]) -> str:
    """Build the loopback base URL for the preview's inner instance.

    The inner instance is the full system_interface server the layout ops act
    on (the wrapper port only serves the chrome page, so it is not the layout
    endpoint). Raises PreviewLayoutError if the state lacks a usable port.
    """
    inner_port = state.get("inner_port")
    # bool is an int subclass but is never a valid port; reject it explicitly.
    if not isinstance(inner_port, int) or isinstance(inner_port, bool):
        raise PreviewLayoutError(
            f"preview state has no usable 'inner_port' (got {inner_port!r}); "
            f"the preview may have failed to boot -- check its log"
        )
    return f"http://127.0.0.1:{inner_port}"


def build_layout_argv(layout_script: Path, layout_args: Sequence[str]) -> list[str]:
    """Build the argv for invoking layout.py with the given subcommand args."""
    return [sys.executable, str(layout_script), *layout_args]


def _subprocess_layout_runner(argv: list[str], cwd: str, env: dict[str, str]) -> int:
    return subprocess.run(argv, cwd=cwd, env=env).returncode


def run(
    slug: str,
    layout_args: Sequence[str],
    repo_root: Path,
    *,
    runner: LayoutRunner = _subprocess_layout_runner,
) -> int:
    """Resolve the preview's port and run layout.py against it.

    Returns layout.py's own exit code on success, or 1 if there is no usable
    preview state for the slug.
    """
    if not layout_args:
        sys.stderr.write(
            "error: no layout subcommand given; e.g. 'preview_layout.py --slug <name> inspect'\n"
        )
        return 1
    try:
        state = load_preview_state(preview_state_path(repo_root, slug))
        base_url = preview_base_url(state)
    except PreviewLayoutError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    env = dict(os.environ)
    env[ENV_WORKSPACE_URL] = base_url
    argv = build_layout_argv(repo_root / LAYOUT_SCRIPT_RELPATH, list(layout_args))
    # cwd = repo root so layout.py's relative ``runtime/applications.toml`` lookup
    # resolves; the preview server is selected via the env override, not cwd.
    return runner(argv, str(repo_root), env)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--slug",
        required=True,
        help="The slug passed to 'reveal_system_interface.py preview'.",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Path to the repository root (default: inferred from this script's location).",
    )
    parser.add_argument(
        "layout_args",
        nargs=argparse.REMAINDER,
        help="The layout.py subcommand and its arguments (e.g. 'open chat:alice').",
    )
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else repo_root_default()
    return run(args.slug, args.layout_args, repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
