#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Generic worker-dispatch driver.

Collapses the launch-task lifecycle steps (mngr create + mngr push of the
runtime dir + mngr message of the task file) into a single invocation, so
callers like ``crystallize-task`` don't have to repeat the boilerplate.

The caller is responsible for writing the task file (with whatever YAML
frontmatter the worker template requires) and for placing it -- and any
gitignored auxiliary state -- under ``runtime/<feature>/<slug>/`` before
calling this script. ``dispatch.py`` orchestrates the lifecycle commands;
it does not compose task content.

Ticket bookkeeping (``tk create`` / ``tk start`` / ``tk close``) is the
caller's responsibility -- it lives in the calling skill's prose so each
flow can shape the ticket title, type, and acceptance criteria itself.

When the worker needs gitignored auxiliary state (scripts, sample data)
that lives outside the runtime dir, the caller declares it in the task
frontmatter with a ``source_artifacts_dir`` key; dispatch reads that key
and pushes the directory alongside the runtime dir -- no extra CLI flag.

Lifecycle commands:

    mngr create <NAME> -t <TEMPLATE> --label workspace=<MINDS_WORKSPACE_NAME>
    mngr push   <NAME>:<RUNTIME_DIR>/   --source <RUNTIME_DIR>/
                --uncommitted-changes=merge
    mngr push   <NAME>:<ARTIFACTS_DIR>/ --source <ARTIFACTS_DIR>/
                --uncommitted-changes=merge   (when frontmatter declares it)
    mngr message <NAME> --message-file <TASK_FILE>

The trailing-slash rewriting and ``--uncommitted-changes=merge`` flag are
required by ``mngr push`` (see ``.agents/shared/references/lead-proxy.md``).

Why ``mngr message`` *after* the pushes (instead of using ``mngr create
--message-file``): if the worker reads its first message before the runtime
dir push lands in its worktree, the task file's ``lead_report_dir`` will
resolve to nothing. Sending the task as a follow-up message guarantees the
worker sees the runtime dir first.

Common-transcript flush: right before sending the task message we invoke the
lead's own ``common_transcript.sh --single-pass`` converter (when present).
This guarantees the worker's first ``mngr transcript <lead>`` read includes
every turn up through the handoff -- the converter normally polls on a 5s
interval, which races with worker startup. It only freshens through the
handoff moment; later lead turns won't appear until the poller catches up,
which is fine for the anchored-lookup pattern (workers locate quotes the
lead already pasted into the task body).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import yaml

_COMMON_TRANSCRIPT_REL = Path("commands/common_transcript.sh")


def _normalize_dir(value: str) -> str:
    """Return ``value`` with exactly one trailing slash."""
    return value.rstrip("/") + "/"


def _read_source_artifacts_dir(task_file: Path) -> Path | None:
    """Return the directory declared by the task frontmatter's
    ``source_artifacts_dir`` key, or ``None`` when the key is absent.

    The caller sets this key when the worker needs gitignored auxiliary
    state that lives outside the runtime dir; dispatch pushes that
    directory alongside the runtime dir. Validating the rest of the
    frontmatter schema is the worker's job (``parse_task_frontmatter.py``);
    here we only pull out this one key, and only raise if it is present
    but not a non-empty string.
    """
    lines = task_file.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end_idx = lines.index("---", 1)
    except ValueError:
        return None
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:end_idx]))
    except yaml.YAMLError:
        return None
    if not isinstance(frontmatter, dict):
        return None
    value = frontmatter.get("source_artifacts_dir")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("frontmatter.source_artifacts_dir must be a non-empty string")
    return Path(value)


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept commands.

    The default implementation calls ``subprocess.run`` directly. Tests
    inject a recording stub instead.
    """

    def run(self, argv: Sequence[str], **kwargs):
        return subprocess.run(list(argv), **kwargs)


def _flush_common_transcript(state_dir: Path | None, runner: Runner) -> None:
    """Run the lead's common-transcript converter once, synchronously.

    No-op when ``state_dir`` is unset (tests, non-mngr environments) or the
    converter script isn't installed at the standard path (non-claude agents
    don't have it). See module docstring for why this runs before the message
    send.

    Best-effort by design: this is a freshness optimization that merely
    races the converter's 5s poller, so a converter failure must not
    abort dispatch (which would orphan a half-launched worker between
    the runtime push and the message send). On non-zero exit we log a
    warning to stderr and let dispatch continue; the worker will see
    whatever the periodic poller has already produced.
    """
    if state_dir is None:
        return
    script = state_dir / _COMMON_TRANSCRIPT_REL
    if not script.is_file():
        return
    result = runner.run([str(script), "--single-pass"], check=False)
    returncode = getattr(result, "returncode", 0)
    if returncode != 0:
        print(
            f"dispatch: warning: common_transcript.sh --single-pass exited "
            f"{returncode}; worker will read whatever the periodic poller "
            f"has already produced",
            file=sys.stderr,
        )


def push(name: str, source_dir: Path, runner: Runner) -> None:
    """Push ``source_dir`` into worker ``name``'s worktree at the same path.

    Uses the directory form (trailing slash on both sides) and
    ``--uncommitted-changes=merge`` -- see lead-proxy.md § "mngr push
    rationale" for why both are required.
    """
    normalized = _normalize_dir(str(source_dir))
    runner.run(
        [
            "mngr",
            "push",
            f"{name}:{normalized}",
            "--source",
            normalized,
            "--uncommitted-changes=merge",
        ],
        check=True,
    )


def dispatch(
    name: str,
    template: str,
    runtime_dir: Path,
    task_file: Path,
    workspace: str,
    state_dir: Path | None = None,
    runner: Runner | None = None,
) -> int:
    """Run the dispatch lifecycle. Returns the process exit code.

    Pre-flight checks (existence of ``runtime_dir``, ``task_file``, and any
    ``source_artifacts_dir`` declared in the task frontmatter) run first so
    a typo doesn't half-create a worker.

    ``state_dir`` is the lead's ``MNGR_AGENT_STATE_DIR``; when set, the
    converter at ``<state_dir>/commands/common_transcript.sh`` is flushed
    before the task message lands so the worker's first transcript read
    sees fresh events.
    """
    runner = runner or Runner()

    if not runtime_dir.is_dir():
        print(
            f"dispatch: --runtime-dir is not a directory: {runtime_dir}",
            file=sys.stderr,
        )
        return 2
    if not task_file.is_file():
        print(f"dispatch: --task-file not found: {task_file}", file=sys.stderr)
        return 2
    try:
        artifacts_dir = _read_source_artifacts_dir(task_file)
    except ValueError as exc:
        print(f"dispatch: {exc}", file=sys.stderr)
        return 2
    if artifacts_dir is not None and not artifacts_dir.is_dir():
        print(
            f"dispatch: source_artifacts_dir is not a directory: {artifacts_dir}",
            file=sys.stderr,
        )
        return 2

    runner.run(
        [
            "mngr",
            "create",
            name,
            "-t",
            template,
            "--label",
            f"workspace={workspace}",
        ],
        check=True,
    )

    push(name, runtime_dir, runner)
    if artifacts_dir is not None:
        push(name, artifacts_dir, runner)

    _flush_common_transcript(state_dir, runner)

    runner.run(
        [
            "mngr",
            "message",
            name,
            "--message-file",
            str(task_file),
        ],
        check=True,
    )

    print(f"dispatch: worker {name} launched and runtime pushed")
    return 0


def main(argv: Sequence[str] | None = None, runner: Runner | None = None) -> int:
    """CLI entry point. Tests inject ``runner`` to capture the argv lifecycle."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--name", required=True, help="Worker name; becomes the mngr/<name> branch."
    )
    parser.add_argument(
        "--template",
        required=True,
        help="mngr create template (e.g. 'worker', 'crystallize-worker').",
    )
    parser.add_argument(
        "--runtime-dir",
        required=True,
        type=Path,
        help="Existing runtime directory pushed verbatim into the worker's worktree.",
    )
    parser.add_argument(
        "--task-file",
        required=True,
        type=Path,
        help="Markdown task file (must already exist; typically inside --runtime-dir).",
    )
    args = parser.parse_args(argv)

    workspace = os.environ.get("MINDS_WORKSPACE_NAME", "default")
    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else None

    return dispatch(
        name=args.name,
        template=args.template,
        runtime_dir=args.runtime_dir,
        task_file=args.task_file,
        workspace=workspace,
        state_dir=state_dir,
        runner=runner,
    )


if __name__ == "__main__":
    sys.exit(main())
