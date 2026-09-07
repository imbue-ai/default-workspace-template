#!/usr/bin/env python3
"""Preview a system-interface worker's branch before it is merged.

The ``preview`` / ``unpreview`` subcommands are thin system-interface adapters
over the shared ``serve_isolated_instance.py`` motion (the previewable-instance
substrate every service flow shares). They hand it the system-interface
specifics -- boot ``uv run system-interface`` from the worker's already-built
``--work-dir`` on a free port, with layout persistence neutered (drop
MNGR_AGENT_ID so it can't clobber the live ``layout.json``) but agent discovery
kept, probe ``/api/agents``, and register the inner app plus the labeled
"preview" wrapper frame the user opens. The shared script owns the ports, the
process/service teardown, and the state file; no fetch, checkout, or rebuild
happens, and the served tree and the worker's folder are never touched. The
worker is a local git-worktree sub-agent whose work_dir is a folder it has
already built, and it must still exist at preview time.

The *apply* step -- landing the merged change and revealing it to the live UI,
with auto-rollback -- belongs to the general update apply,
``.agents/skills/update-self/scripts/update_self.py apply``, which owns the
whole reveal machinery (snapshots, dependency refresh, pre-flight, health
probes, rollback, the interruption marker) for every update flow. This script
covers only the pre-merge preview, whose gating on the user's judgment is the
non-deterministic part that stays with the agent.

Run via bare ``python3`` (stdlib-only) -- it orchestrates the environment, so
it must not depend on any particular venv being synced.

Usage:
    python3 reveal_system_interface.py preview --slug <name> --work-dir <worker-work-dir> [--repo-root PATH]
    python3 reveal_system_interface.py unpreview --slug <name> [--repo-root PATH]

Environment:
    MNGR_AGENT_ID  Dropped for the preview boot so it cannot clobber the live
                   layout.

Exit codes:
    0  Success (preview is up / torn down).
    1  The preview failed to boot (and tore itself down), or names a
       work_dir that carries no built bundle.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence

# The served app and its build output. The preview serves the worker's app dir
# as-is and will not build for it: a work_dir without the gitignored bundle is
# a worker that skipped its build, and the preview refuses it rather than boot
# the backend's "Frontend not built" placeholder.
SYSTEM_INTERFACE_DIR = "system/apps/system_interface"
STATIC_DIR = f"{SYSTEM_INTERFACE_DIR}/imbue/system_interface/static"
FRONTEND_BUILD_INDEX = f"{STATIC_DIR}/index.html"
TOOL_NAME = "system-interface"

ENV_MNGR_AGENT_ID = "MNGR_AGENT_ID"

# The deterministic boot + teardown of a previewable instance is the shared
# ``serve_isolated_instance.py`` motion that every service flow reuses. It
# lives two levels up under ``.agents/shared/scripts/`` and is stdlib-only, so
# it runs under the same interpreter as this script.
_SHARED_SERVE_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "shared"
    / "scripts"
    / "serve_isolated_instance.py"
)
# The service names the preview registers: the inner booted app and the outer
# wrapper the user actually opens. Fixed because the flow runs one preview at a
# time -- enforced by the guard in ``preview`` (a different slug's live preview
# refuses to boot); a re-run of the *same* slug is fine because the shared
# script clears its own stale instance first.
PREVIEW_INNER_SERVICE_NAME = "si-preview-app"
PREVIEW_SERVICE_NAME = "si-preview"
# Where the shared script files each instance's state (mirrors its STATE_ROOT /
# STATE_FILENAME). Used only to detect a different slug's live preview.
_INSTANCES_ROOT = "data/.state/isolated-instances"
_INSTANCE_STATE_FILENAME = "instance.json"
# The system interface reads its bind host/port from the environment; the
# shared script injects the free port into PORT and 127.0.0.1 into HOST.
PREVIEW_PORT_ENV = "SYSTEM_INTERFACE_PORT"
PREVIEW_HOST_ENV = "SYSTEM_INTERFACE_HOST"
# ``/api/agents`` exercises the mngr plugin discovery path, so a 200 there is a
# strong "the backend actually works" signal; handed to the shared preview
# script as its ``--health-path``.
HEALTH_PATH = "/api/agents"


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept commands."""

    def run(self, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(list(argv), **kwargs)


def _preview_instance_name(slug: str) -> str:
    """The name the shared script files this preview's instance under (its state
    dir + the stable id ``unpreview`` tears down). One preview per slug."""
    return f"{PREVIEW_SERVICE_NAME}-{slug}"


def _find_other_preview(repo_root: Path, slug: str) -> str | None:
    """Return another slug's live preview instance name, or ``None``.

    Only a *different* slug's preview blocks: both would register the same
    fixed service names, so booting a second one hijacks the first's tab.
    Re-running the same slug stays allowed -- the shared script clears its own
    stale instance, which is the normal retry path.
    """
    instances_root = repo_root / _INSTANCES_ROOT
    if not instances_root.is_dir():
        return None
    own_name = _preview_instance_name(slug)
    prefix = f"{PREVIEW_SERVICE_NAME}-"
    for state_dir in sorted(instances_root.iterdir()):
        if not state_dir.name.startswith(prefix) or state_dir.name == own_name:
            continue
        if (state_dir / _INSTANCE_STATE_FILENAME).exists():
            return state_dir.name
    return None


def preview(slug: str, work_dir: str, repo_root: Path, *, runner: Runner) -> int:
    """Stand up a pre-merge preview of the worker's ``work_dir``.

    Thin system-interface adapter over the shared ``serve_isolated_instance.py``
    ``up`` motion: validate the worker's app dir, require that the worker built
    its frontend bundle, then hand the shared script the system-interface
    specifics -- boot ``uv run system-interface`` from the worker's
    already-built app dir on a free port; neuter layout persistence by dropping
    MNGR_AGENT_ID (so the preview can't clobber the live ``layout.json``) while
    keeping discovery, so the real conversations still render; probe
    ``/api/agents``; register the inner app and the labeled wrapper frame.
    ``work_dir`` must still exist -- run this before the worker is destroyed.
    """
    # Sanity-check the work_dir before disturbing anything: a wrong --work-dir
    # should fail fast rather than reaching the shared script.
    worker_app_dir = Path(work_dir) / SYSTEM_INTERFACE_DIR
    if not worker_app_dir.is_dir():
        sys.stderr.write(
            f"preview: {worker_app_dir} is not a directory; is --work-dir correct "
            "and is the worker still alive (not destroyed)?\n"
        )
        return 1
    # The preview serves the worker's app dir as-is; it does not build for the
    # worker. A work_dir without a frontend bundle means the worker reported
    # done without building it, so booting would only serve the backend's
    # "Frontend not built" placeholder -- a dead preview that reads as working.
    if not (Path(work_dir) / FRONTEND_BUILD_INDEX).exists():
        sys.stderr.write(
            f"preview: no frontend build in {work_dir} "
            f"({FRONTEND_BUILD_INDEX} is missing), so the preview would serve the "
            "'Frontend not built' placeholder. The worker must build the frontend "
            "(cd system/apps/system_interface/frontend && npm ci && npm run build) before "
            "its work_dir can be previewed -- re-brief it to build, then retry.\n"
        )
        return 1
    other = _find_other_preview(repo_root, slug)
    if other is not None:
        other_slug = other.removeprefix(f"{PREVIEW_SERVICE_NAME}-")
        sys.stderr.write(
            f"preview: another pass's preview is already up ({other}); the "
            f"'{PREVIEW_SERVICE_NAME}' tab can only show one at a time, so booting "
            "this one would hijack it. Surface this to the user and coordinate "
            "with that pass -- or, if it is abandoned, tear it down first with "
            f"'unpreview --slug {other_slug}'.\n"
        )
        return 1
    result = runner.run(
        [
            sys.executable,
            str(_SHARED_SERVE_SCRIPT),
            "up",
            "--name",
            _preview_instance_name(slug),
            "--cwd",
            str(worker_app_dir),
            "--port-env",
            PREVIEW_PORT_ENV,
            "--host-env",
            PREVIEW_HOST_ENV,
            "--unset-env",
            ENV_MNGR_AGENT_ID,
            "--health-path",
            HEALTH_PATH,
            "--service-name",
            PREVIEW_INNER_SERVICE_NAME,
            "--preview-service-name",
            PREVIEW_SERVICE_NAME,
            "--preview-title",
            slug,
            "--repo-root",
            str(repo_root),
            "--",
            "uv",
            "run",
            TOOL_NAME,
        ],
        cwd=str(repo_root),
        check=False,
    )
    return int(getattr(result, "returncode", 0))


def unpreview(slug: str, repo_root: Path, *, runner: Runner) -> int:
    """Tear down the preview for ``slug`` via the shared script. Idempotent: a
    missing instance is a no-op success, so this is safe on reject, after a
    successful apply, or to recover from a half-set-up preview."""
    result = runner.run(
        [
            sys.executable,
            str(_SHARED_SERVE_SCRIPT),
            "down",
            "--name",
            _preview_instance_name(slug),
            "--repo-root",
            str(repo_root),
        ],
        cwd=str(repo_root),
        check=False,
    )
    return int(getattr(result, "returncode", 0))


def _add_repo_root_arg(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root (default: current directory).",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Preview a system-interface worker branch before merging, and tear "
            "the preview down. (Landing and revealing a merged change is the "
            "general update apply: update_self.py apply.)"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview_parser = subparsers.add_parser(
        "preview",
        help="Boot the worker's already-built work_dir and serve it as a "
        "previewable tab, before any merge.",
    )
    preview_parser.add_argument(
        "--slug",
        required=True,
        help="Short kebab-case id for this preview (names the service/state dir).",
    )
    preview_parser.add_argument(
        "--work-dir",
        required=True,
        help="The worker's work_dir (from `mngr ls --include 'name==\"<worker>\"' "
        "--format json` -> agent.work_dir). The worker must still exist.",
    )
    _add_repo_root_arg(preview_parser)

    unpreview_parser = subparsers.add_parser(
        "unpreview",
        help="Tear down a preview (kill the server, deregister the service). Idempotent.",
    )
    unpreview_parser.add_argument(
        "--slug", required=True, help="The slug passed to 'preview'."
    )
    _add_repo_root_arg(unpreview_parser)

    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    if args.command == "preview":
        return preview(args.slug, args.work_dir, repo_root, runner=Runner())
    if args.command == "unpreview":
        return unpreview(args.slug, repo_root, runner=Runner())
    parser.error(f"unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
