#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Worker-creation driver for the launch-task family of skills.

The dispatch contract this script carries is *level-agnostic*: "lead" and
"worker" are roles in a single dispatch, not fixed positions in a hierarchy.
Any agent that runs ``launch`` and polls for a report is a lead -- a chat agent
at the top, or a worker that dispatches work of its own. Nothing here depends
on which level the launching agent sits at, so a worker launches a sub-worker
with exactly the commands a chat agent uses.

Two stamps in the task file's frontmatter make that work without any path or
address being re-derived downstream:

``lead_agent``
    The launching agent's own name (its ``MNGR_AGENT_NAME``), so the worker
    knows exactly which agent to push its report to. The same value is also
    attached to the worker's agent as a ``--label lead_agent=<lead>``, so a
    lead can find its own workers -- and its workers' workers -- in
    ``mngr list`` without consulting any task file. A second label,
    ``runtime_dir=<repo-relative runtime dir>``, records where the worker's
    task file and reports live, so ``destroy`` can carry a descendant's runtime
    dir upward without opening any task file either.

``task_file``
    The task file's own path, relative to the repo root. The worker receives
    the task file as its message and reads that exact path back out of it, so
    it never has to guess where its task file landed. Exact paths are what
    keep nesting unambiguous: two levels of dispatch can use the same
    directory names without colliding.

Five subcommands cover the lead-side lifecycle, and one (``report``) the
worker side:

``launch``
    Runs the worker-creation lifecycle synchronously (``mngr create`` + the
    runtime-dir sync + the task message) and returns. Callers run this in the
    *foreground* so a failed launch surfaces immediately rather than as a
    delayed background notification.

``await``
    Reads the ``finish_report_path`` field from the task file's frontmatter and
    blocks until that file appears, prints its contents to stdout, moves the
    report into ``<dir>/consumed/`` under a timestamped name, and returns 0. It
    also watches the ``milestones/`` directory beside that file, where a worker
    drops non-blocking milestone reports: one with no same-named entry in
    ``consumed/`` ends the poll the same way (contents on stdout, exit 0), is
    archived under its own name, and its path is named on stderr.
    ``report.md`` wins when both are present. On timeout it returns non-zero so
    the caller drops into the liveness diagnosis described in
    ``.agents/shared/references/lead-proxy.md``. Callers run this in the
    *background* and then **end the turn**: the completion wakes the agent and
    carries the report, so sleeping against it only adds the gap between the
    report landing and the sleep expiring (see ``lead-proxy.md``, "Never sleep
    on a worker"). Re-invoke it once per gate cycle. The archive step is what
    makes re-invocation safe: ``launch`` refuses to start while anything sits at
    ``finish_report_path`` (or an unconsumed milestone beside it), so a relaunch
    after a gate would otherwise trip that guard until the lead moved the file
    aside by hand. Deciding answer-vs-escalate and merging remain lead judgment
    and stay in ``lead-proxy.md``.

``report``
    The worker side of the same contract: writes ``report.md`` beside the task
    file's ``finish_report_path`` in the *worker's own* tree and delivers it to
    the lead. A worker never re-derives the rsync invocation or the fallback in
    prose; it calls this and gets either a delivery or a loud failure.

``launch-sync``
    The blocking one-call path for non-interactive callers (services): launch,
    wait for the report in the *foreground*, emit a structured-result JSON
    object (``timed_out`` plus the report ``type``/``name``/``body`` and the
    worker ``branch``), and destroy the worker. ``--result-json`` also writes
    that JSON to a caller-named file as the machine-readable contract.
    ``--keep-agent`` skips the destroy; a timeout never destroys (the report may
    still be coming).

``destroy``
    Destroys the worker agent and, by default, every worker underneath it (the
    agents whose ``lead_agent`` chain reaches it), deepest first. Before each
    agent goes, the runtime dirs of the workers *it* dispatched are pulled out
    of its worktree into the caller's tree at the same repo-relative paths, so
    the task files and consumed reports of the whole subtree survive.
    Unmerged commits and a dirty worktree are printed as a warning, never a
    refusal. Branches ``mngr/<name>`` survive unless ``--delete-branches`` is
    passed; mngr preserves each destroyed agent's transcript under
    ``$MNGR_HOST_DIR/preserved/``. One outcome line per agent goes to stderr;
    the exit code is non-zero if any agent failed. ``--no-recursive`` destroys
    the one agent and names each descendant it leaves behind.

``stop``
    Stops the worker agent and, by default, its whole subtree (children first)
    with ``mngr stop --archive``: processes go, worktrees and branches stay,
    and the ``archived_at`` label marks each one as stopped on purpose rather
    than crashed. This is what a lead does with a worker that failed.
    ``--no-recursive`` stops the one agent.

The ``launch`` / ``await`` / ``launch-sync`` subcommands take the same
``--task-file``: ``launch`` sends it to the worker, and ``await`` /
``launch-sync`` read its ``finish_report_path`` to learn what to wait for.
Putting the wait target in frontmatter (rather than deriving a fixed path) keeps
the contract data-driven, so future flows can point the wait at a different
report without a code change.

The caller is responsible for writing the task file (with whatever YAML
frontmatter the worker template requires) and for placing it -- and any
gitignored auxiliary state -- under ``data/.tasks/<feature>/<slug>/`` before
calling ``launch``. This script orchestrates the lifecycle commands; it does
not compose task content.

Ticket bookkeeping (``tk create`` / ``tk start`` / ``tk close``) is the
caller's responsibility -- it lives in the calling skill's prose so each
flow can shape the ticket title, type, and acceptance criteria itself.

When the worker needs gitignored auxiliary state (scripts, sample data)
that lives outside the runtime dir, the caller declares it in the task
frontmatter with a ``source_artifacts_dir`` key; launch reads that key
and syncs the directory alongside the runtime dir -- no extra CLI flag.

Launch lifecycle commands:

    mngr create <NAME> -t <TEMPLATE> --label agent_created=true
                                     --label lead_agent=<LEAD>
                                     --label runtime_dir=<RUNTIME_DIR>
    mngr rsync  ./<RUNTIME_DIR>/   <NAME>:<RUNTIME_DIR>/   --uncommitted-changes=clobber
    mngr rsync  ./<ARTIFACTS_DIR>/ <NAME>:<ARTIFACTS_DIR>/ --uncommitted-changes=clobber
                (when frontmatter declares it)
    mngr message <NAME> --message-file <TASK_FILE>

Report delivery (the worker's side, run by ``report``):

    mngr rsync  ./<REPORT_DIR>/ <LEAD>:<REPORT_DIR>/ --uncommitted-changes=clobber
    mngr list   --format jsonl --on-error continue    (fallback resolution only)

Teardown commands (``destroy`` and ``stop``; the subtree comes from one
``mngr list``):

    mngr rsync   <NAME>:data/.tasks/ ./data/.tasks/ --uncommitted-changes=clobber
                 -- --update --exclude=/<NAME's own runtime dir under data/.tasks>
                 (once per destroyed agent, pulling its workers' runtime dirs out)
    mngr destroy <NAME> --force [-b]     (``-b`` only with ``--delete-branches``)
    mngr stop    <NAME> --archive

``mngr rsync`` takes ``SOURCE DESTINATION`` (the local source dir first, then
the ``<NAME>:<PATH>`` agent endpoint). The trailing slash on both ends makes
rsync copy directory *contents* into the destination. The local source is
``./``-prefixed so mngr reads it as a path rather than an agent name, while the
agent destination stays repo-relative so mngr resolves it against the worker's
workdir. The destination is always a runtime dir under gitignored ``data/``, so
``--uncommitted-changes=clobber`` is the right mode: there is nothing tracked at
the destination for the sync to overwrite, and unlike ``merge`` it does not run
``git stash push -u`` / pop on the worker's tree. That matters because the git
stash stack is shared by every worktree of the repo: two dispatches syncing at
once (a lead and one of its workers, or two siblings) would pop each other's
entries and corrupt both trees.

Why ``mngr message`` *after* the syncs (instead of using ``mngr create
--message-file``): if the worker reads its first message before the runtime
dir sync lands in its worktree, the task file's ``finish_report_path`` will
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
import datetime
import functools
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Sequence, TextIO

import yaml

_COMMON_TRANSCRIPT_REL = Path("commands/common_transcript.sh")

_DEFAULT_TIMEOUT = "30m"
_DEFAULT_POLL_INTERVAL = "5s"

# Distinct exit code for an await that timed out without the report appearing,
# matching coreutils ``timeout``'s convention so the prose's mental model
# carries over.
_AWAIT_TIMEOUT_RC = 124
# Distinct exit code for an await that stopped early because the worker's own
# agent was shed by the OOM daemon (so it will never report until revived).
# Separate from the timeout code so the lead can tell "paused for memory" apart
# from "still running, just slow".
_AWAIT_SHED_RC = 75
# Distinct exit code for an await that stopped early because the worker's agent
# went idle (ended its turn) without the report ever appearing -- a finished or
# stalled worker whose delivery failed will never report, so waiting out the
# full timeout only hides the problem. The message points at the worker's own
# worktree, where an undelivered report usually sits.
_AWAIT_IDLE_RC = 76
# Consecutive idle observations required before concluding the worker ended its
# turn without reporting. Multiple observations (spaced by the poll interval)
# absorb the race where the worker is mid-delivery: the report file is checked
# first on every loop, so a delivered report always wins.
_IDLE_POLLS_BEFORE_GIVING_UP = 3


def _normalize_dir(value: str) -> str:
    """Return ``value`` with exactly one trailing slash."""
    return value.rstrip("/") + "/"


def _parse_duration(value: str) -> float:
    """Parse a duration like ``30m``, ``90s``, ``1h``, or a bare integer (seconds).

    Mirrors the ``timeout 30m`` idiom the lead-proxy prose used before this was
    a script, so the same values keep working.
    """
    text = value.strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("duration must not be empty")
    units = {"s": 1, "m": 60, "h": 3600}
    unit = units.get(text[-1])
    number = text[:-1] if unit is not None else text
    multiplier = unit if unit is not None else 1
    try:
        magnitude = float(number)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid duration {value!r}; use e.g. '30m', '90s', '1h', or seconds"
        )
    if magnitude <= 0:
        raise argparse.ArgumentTypeError(f"duration must be positive: {value!r}")
    return magnitude * multiplier


def _split_frontmatter(text: str) -> tuple[dict[str, object] | None, str]:
    """Split leading YAML frontmatter from the body.

    Returns ``(frontmatter, body)``. ``frontmatter`` is ``None`` when there is no
    leading ``---`` fence, the closing fence is missing, or the parsed YAML is not
    a mapping; otherwise it is the parsed mapping. ``body`` is the text below the
    closing fence (``""`` when there is no fence). Raises ``yaml.YAMLError`` when a
    fenced block contains invalid YAML -- callers decide whether to surface that
    (an authoring bug in a deterministic input) or swallow it (tolerant parsing of
    agent-authored runtime output). The shared scan/parse for both callers.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, ""
    try:
        end_idx = lines.index("---", 1)
    except ValueError:
        return None, ""
    frontmatter = yaml.safe_load("\n".join(lines[1:end_idx]))
    body = "\n".join(lines[end_idx + 1 :]).strip("\n")
    if not isinstance(frontmatter, dict):
        return None, body
    return frontmatter, body


def _read_frontmatter_field(task_file: Path, key: str) -> str | None:
    """Return the string value of frontmatter ``key``, or ``None`` if absent.

    Returns ``None`` when the file has no frontmatter block (no leading ``---``)
    or the key is missing -- full schema validation is the worker's job
    (``parse_task_frontmatter.py``); here we pull out one key at a time.

    Raises ``ValueError`` when the frontmatter is genuinely malformed -- a
    present block whose body is invalid YAML, or a key present but not a
    non-empty string. A broken frontmatter block is an authoring bug in the
    task file, so we surface it (with the original parse error chained) rather
    than silently degrading it to "field absent" and launching the worker with
    the wrong inputs.
    """
    try:
        frontmatter, _body = _split_frontmatter(task_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(
            f"{task_file}: frontmatter block is present but contains invalid YAML"
        ) from exc
    if frontmatter is None:
        return None
    value = frontmatter.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"frontmatter.{key} must be a non-empty string")
    return value


def _read_source_artifacts_dir(task_file: Path) -> Path | None:
    """Return the optional ``source_artifacts_dir`` declared in the task
    frontmatter, or ``None`` when absent.

    The caller sets this key when the worker needs gitignored auxiliary state
    that lives outside the runtime dir; launch syncs that directory alongside
    the runtime dir.
    """
    value = _read_frontmatter_field(task_file, "source_artifacts_dir")
    return Path(value) if value is not None else None


def _read_finish_report_path(task_file: Path) -> Path:
    """Return the ``finish_report_path`` the worker writes its report to.

    This is the file ``await`` polls for. Required: raises ``ValueError`` if
    the key is absent (or present but not a non-empty string).
    """
    value = _read_frontmatter_field(task_file, "finish_report_path")
    if value is None:
        raise ValueError("frontmatter is missing required field `finish_report_path`")
    return Path(value)


def _set_frontmatter_field(text: str, key: str, value: str) -> str:
    """Return ``text`` with frontmatter ``key`` set to ``value``.

    Replaces the existing ``key:`` line in place (preserving the rest of the
    file verbatim) or, if absent, inserts it just after the opening ``---``.
    Returns ``text`` unchanged when there is no frontmatter block.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return text
    for i in range(1, end):
        if lines[i].split(":", 1)[0].strip() == key:
            lines[i] = f"{key}: {value}"
            return "\n".join(lines)
    lines.insert(1, f"{key}: {value}")
    return "\n".join(lines)


class _LeadResolution(NamedTuple):
    """The outcome of resolving (and stamping) the lead in a task file.

    Exactly one of the two fields carries the answer: ``exit_code`` is set when
    the lead could not be resolved at all (launch must abort with it), and
    ``lead_name`` is set when it could. Both are ``None`` in the one benign
    case where there is nothing to stamp and nothing to fail on -- a task file
    with no frontmatter block at all, which launch tolerates (schema validation
    is the worker's job) and which simply yields no lead label.
    """

    lead_name: str | None
    exit_code: int | None


def _ensure_lead_agent(task_file: Path) -> _LeadResolution:
    """Stamp the launching agent as the report recipient in the task file.

    The agent running ``launch`` *is* the lead that polls for the worker's
    report, so its own ``MNGR_AGENT_NAME`` is the authoritative ``lead_agent`` --
    we fill it in (overwriting whatever the file holds) from the environment
    rather than trusting the task file. That frees task-file authors from setting
    the field at all and eliminates a silent-failure class: a literal,
    unexpanded ``$MNGR_AGENT_NAME`` (or a stale/omitted value) used to leave the
    worker with no valid address, so it could not rsync its report back and the
    lead's poll waited forever.

    When ``MNGR_AGENT_NAME`` is unset -- i.e. ``launch`` is running outside an
    mngr agent, as in a manual invocation or a test -- the file's existing value
    is used as a fallback; an unresolved value (missing, blank, or an unexpanded
    ``$...``) in that case is fatal (exit 2) rather than launching an
    unaddressable worker.

    Returns the resolved lead's name, which launch also attaches to the worker
    as a ``lead_agent`` label -- the launcher resolves it once and both uses
    read the same answer. On unrecoverable misconfiguration the returned
    ``exit_code`` is ``2`` instead.
    """
    text = task_file.read_text(encoding="utf-8")
    # Invalid frontmatter YAML has already raised in launch's preflight
    # (``_read_source_artifacts_dir``), so any error here is a genuine bug and
    # is allowed to propagate.
    frontmatter, _body = _split_frontmatter(text)
    if frontmatter is None:
        return _LeadResolution(lead_name=None, exit_code=None)
    current = frontmatter.get("lead_agent")
    lead_name = os.environ.get("MNGR_AGENT_NAME")
    if lead_name:
        if current != lead_name:
            task_file.write_text(
                _set_frontmatter_field(text, "lead_agent", lead_name), encoding="utf-8"
            )
            print(
                f"create_worker: set lead_agent to {lead_name!r} (was {current!r})",
                file=sys.stderr,
            )
        return _LeadResolution(lead_name=lead_name, exit_code=None)
    # No launcher identity in the environment: fall back to the file's own value.
    if isinstance(current, str) and current.strip() and "$" not in current:
        return _LeadResolution(lead_name=current, exit_code=None)
    print(
        "create_worker: lead_agent is unresolved "
        f"({current!r}) and MNGR_AGENT_NAME is unset -- the worker would have no "
        "address to send its report to.",
        file=sys.stderr,
    )
    return _LeadResolution(lead_name=None, exit_code=2)


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
    abort launch (which would orphan a half-launched worker between
    the runtime sync and the message send). On non-zero exit we log a
    warning to stderr and let launch continue; the worker will see
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
            f"create_worker: warning: common_transcript.sh --single-pass exited "
            f"{returncode}; worker will read whatever the periodic poller "
            f"has already produced",
            file=sys.stderr,
        )


# Hard ceilings on the teardown's external commands, so an unattended
# ``launch-sync`` cannot hang on one of them: a pull over SSH, a git probe, one
# ``mngr destroy``/``mngr stop``.
_RSYNC_PULL_TIMEOUT_SECONDS = 300.0
_GIT_PROBE_TIMEOUT_SECONDS = 15.0
_MNGR_LIFECYCLE_TIMEOUT_SECONDS = 300.0


def _git_output(argv: Sequence[str], runner: Runner) -> str | None:
    """``argv``'s stdout, stripped -- or ``None`` when the command failed.

    Every git probe here is advisory (a failure means "no evidence", never a
    refusal), so the failure mode folds into the return value.
    """
    try:
        result = runner.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if getattr(result, "returncode", 0) != 0:
        return None
    return (getattr(result, "stdout", "") or "").strip()


def _repo_toplevel(runner: Runner) -> Path | None:
    """The repo root per ``git rev-parse --show-toplevel``, or ``None``.

    Asked of git rather than derived from this script's location, since launch
    may run from any subdirectory. ``None`` when git cannot answer.
    """
    toplevel = _git_output(["git", "rev-parse", "--show-toplevel"], runner)
    return Path(toplevel) if toplevel else None


def _repo_relative_path(path: Path, toplevel: Path | None) -> str:
    """``path`` relative to the repo root ``toplevel``, as a POSIX string.

    Every path that crosses from one agent's worktree to another's is resolved
    against *that* agent's worktree root, so anything cwd-relative would break
    the moment a lead launches from somewhere other than the root. Falls back
    to the path exactly as given (POSIX-normalized) when there is no repo root
    or the path lives outside it.
    """
    if toplevel is None:
        return path.as_posix()
    try:
        relative = path.resolve().relative_to(toplevel.resolve())
    except ValueError:
        return path.as_posix()
    return relative.as_posix()


def _local_rsync_path(rel: str) -> str:
    """``rel`` spelled so ``mngr rsync`` reads it as a local path.

    ``mngr rsync`` only treats a path starting with ``/``, ``./``, ``../`` or
    ``~/`` as local; a bare ``data/foo/`` would be misparsed as an *agent name*.
    """
    return rel if rel.startswith(("/", "./", "../", "~/")) else f"./{rel}"


def _rsync_endpoints(source_dir: Path, toplevel: Path | None) -> tuple[str, str]:
    """The ``(local, agent)`` spellings of ``source_dir`` for one ``mngr rsync``.

    The agent side is the repo-relative path: mngr resolves a relative agent
    ``:PATH`` against that agent's worktree root, so the dir lands at the same
    place inside the worker wherever the caller is running. The local side is
    the same dir under ``toplevel`` when the repo root is known, so both sides
    agree even when launch runs from a subdirectory; without a root, the path
    as given. Both carry the trailing slash (copy contents, not the dir).
    """
    if toplevel is not None:
        try:
            rel = source_dir.resolve().relative_to(toplevel.resolve()).as_posix()
        except ValueError:
            rel = None
        if rel is not None:
            rel = _normalize_dir(rel)
            return _normalize_dir(str(toplevel / rel)), rel
    rel = _normalize_dir(source_dir.as_posix())
    return _local_rsync_path(rel), rel


def rsync_dir(
    name: str, source_dir: Path, runner: Runner, toplevel: Path | None = None
) -> None:
    """Rsync ``source_dir`` into agent ``name``'s worktree at the same path.

    Nothing here is specific to the *direction* of a dispatch: ``launch`` uses
    it to push the runtime dir down to a worker, and ``report`` uses it to push
    the report dir back up to a lead. Both sides address the same repo-relative
    path (``_rsync_endpoints``), which is what makes one helper serve both.

    ``--uncommitted-changes=clobber`` rather than ``merge``: every directory
    synced this way is a runtime dir under gitignored ``data/``, so there is
    nothing tracked at the destination that overwriting could lose. ``merge``
    would instead wrap the sync in ``git stash push -u`` and a pop -- and the
    git stash stack is shared by every worktree of the repo, so two dispatches
    syncing at the same time (a lead and one of its own workers, or two
    siblings) can pop each other's entries and leave both trees wrong.
    """
    local, rel = _rsync_endpoints(source_dir, toplevel)
    runner.run(
        ["mngr", "rsync", local, f"{name}:{rel}", "--uncommitted-changes=clobber"],
        check=True,
    )


def rsync_dir_from(
    name: str,
    source_dir: Path,
    runner: Runner,
    toplevel: Path | None,
    excludes: Sequence[str] = (),
) -> bool:
    """Rsync repo-relative ``source_dir`` out of agent ``name``'s worktree into
    the same path under the caller's repo root; returns whether it landed.

    The pull form of ``rsync_dir``: the agent endpoint is the SOURCE and the
    local path the DESTINATION, same trailing slashes, same ``clobber``. The
    destination may already exist, so rsync merges into it, ``--update`` keeps
    the newer copy of any file present on both sides, and each of ``excludes``
    (an rsync pattern, relative to ``source_dir``) is left out. ``mngr rsync``
    needs only the *host* to be reachable, so the source agent may be STOPPED.
    A failure (non-zero, or the hard timeout) is returned, not raised.
    """
    rel = _normalize_dir(source_dir.as_posix())
    local = _normalize_dir(str(toplevel / rel)) if toplevel else _local_rsync_path(rel)
    try:
        result = runner.run(
            [
                "mngr",
                "rsync",
                f"{name}:{rel}",
                local,
                "--uncommitted-changes=clobber",
                "--",
                "--update",
                *(f"--exclude={pattern}" for pattern in excludes),
            ],
            check=False,
            timeout=_RSYNC_PULL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False
    return getattr(result, "returncode", 0) == 0


def _ensure_task_file_path(task_file: Path, toplevel: Path | None) -> None:
    """Stamp the task file's own repo-relative path into its frontmatter.

    The worker receives the task file as the body of its first message, so
    without this it would have to guess where the copy on disk landed. With it,
    the worker reads its exact path straight out of the message it was handed
    -- which is what lets two levels of dispatch (or two siblings) use the same
    directory names without either one resolving the other's task file.

    A file with no frontmatter block is left untouched, matching
    ``_set_frontmatter_field``: launch tolerates a body-only task file and
    leaves schema validation to the worker's parser.
    """
    text = task_file.read_text(encoding="utf-8")
    stamped = _set_frontmatter_field(
        text, "task_file", _repo_relative_path(task_file, toplevel)
    )
    if stamped == text:
        return
    task_file.write_text(stamped, encoding="utf-8")


def _worktree_is_clean(runner: Runner) -> bool:
    """Whether the current git working tree has no uncommitted changes.

    The worker is created from the lead's committed HEAD (``mngr create``
    branches from the current commit and defaults to ``--ensure-clean``), so any
    uncommitted change in the lead's tree is invisible to the worker *and* makes
    the subsequent ``mngr create`` abort outright. We check up front through the
    same ``git status --porcelain`` mngr uses, so a dirty tree surfaces as an
    actionable message rather than an opaque ``CalledProcessError``.

    Returns ``True`` (clean -- proceed) when git reports no changes, and also
    when the command fails (not a git repo, or git unavailable): there is
    nothing for us to gate on, and if ``mngr create`` later needs a repo it
    surfaces its own error. Any porcelain output -- including untracked files,
    which mngr also treats as dirty -- means dirty.
    """
    result = runner.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if getattr(result, "returncode", 0) != 0:
        return True
    return not (getattr(result, "stdout", "") or "").strip()


def launch(
    name: str,
    template: str,
    runtime_dir: Path,
    task_file: Path,
    state_dir: Path | None = None,
    runner: Runner | None = None,
) -> int:
    """Run the worker-creation lifecycle. Returns the process exit code.

    Pre-flight checks run first so a typo doesn't half-create a worker:
    ``runtime_dir`` and ``task_file`` existence (and any declared
    ``source_artifacts_dir``'s existence) return exit code 2 with a clean
    message, since those are caller-supplied paths. So does a leftover file at
    the task's ``finish_report_path`` -- a stale report from a previous run
    would satisfy ``await`` instantly, so launch refuses until the caller has
    confirmed it was handled and moved it aside (likewise an unconsumed
    milestone beside it). So does a dirty working tree:
    the worker branches from committed HEAD, so uncommitted changes never reach
    it (and ``mngr create`` refuses a dirty tree anyway) -- launch stops with an
    actionable "commit first" message rather than letting that surface as an
    opaque ``mngr create`` failure. Malformed task-file frontmatter instead
    raises ``ValueError`` (full traceback) -- that's a bug in how the task file
    was composed, not a bad CLI argument.

    ``state_dir`` is the lead's ``MNGR_AGENT_STATE_DIR``; when set, the
    converter at ``<state_dir>/commands/common_transcript.sh`` is flushed
    before the task message lands so the worker's first transcript read
    sees fresh events.
    """
    runner = runner or Runner()

    if not runtime_dir.is_dir():
        print(
            f"create_worker: --runtime-dir is not a directory: {runtime_dir}",
            file=sys.stderr,
        )
        return 2
    if not task_file.is_file():
        print(f"create_worker: --task-file not found: {task_file}", file=sys.stderr)
        return 2
    # A malformed ``source_artifacts_dir`` (or invalid frontmatter YAML) is an
    # authoring bug in the task file -- let it raise so the caller gets the full
    # traceback rather than a terse one-line message. The CLI path checks above
    # stay as clean exit-2 validations (those are caller-supplied arguments, not
    # file content).
    artifacts_dir = _read_source_artifacts_dir(task_file)
    if artifacts_dir is not None and not artifacts_dir.is_dir():
        print(
            f"create_worker: source_artifacts_dir is not a directory: {artifacts_dir}",
            file=sys.stderr,
        )
        return 2
    # A leftover report at ``finish_report_path`` would satisfy the next
    # ``await`` instantly (it only polls for file existence), so the new
    # worker's real result would never be read -- and the runtime-dir sync
    # below would even copy the stale report into the new worker's worktree.
    # Refuse to launch; the caller must confirm the old report was fully
    # handled and move it aside before relaunching.
    report_path_value = _read_frontmatter_field(task_file, "finish_report_path")
    if report_path_value is not None:
        report_path = Path(report_path_value)
        consumed_dir = _consumed_dir(report_path)
        if report_path.exists():
            print(
                f"create_worker: refusing to launch {name}: something already "
                f"exists at the report path {report_path_value} (left over from a "
                f"previous run; `await` would return it instantly instead of this "
                f"worker's real report). Confirm it has been dealt with, move it "
                f"aside (e.g. mkdir -p {consumed_dir} && mv {report_path_value} "
                f"{consumed_dir}/), then relaunch.",
                file=sys.stderr,
            )
            return 2
        # An unconsumed milestone is stale for the same reason: ``await`` would
        # return it as the new worker's news.
        stale_milestones = _unconsumed_milestones(report_path)
        if stale_milestones:
            listed = ", ".join(str(path) for path in stale_milestones)
            print(
                f"create_worker: refusing to launch {name}: unconsumed "
                f"milestone report(s) are still waiting beside the report path "
                f"({listed}); `await` would return one instantly instead of "
                f"this worker's real report. Confirm each has been dealt with, "
                f"move it aside (e.g. mkdir -p {consumed_dir} && mv "
                f"{_milestones_dir(report_path)}/*.md {consumed_dir}/), then "
                f"relaunch.",
                file=sys.stderr,
            )
            return 2

    # A dirty working tree is fatal: the worker is created from committed HEAD,
    # so uncommitted changes never reach it, and ``mngr create`` refuses a dirty
    # tree regardless. Catch it here with an actionable message. Commit -- never
    # stash: stashed work silently drops out of multi-agent coordination and
    # gets lost.
    if not _worktree_is_clean(runner):
        print(
            f"create_worker: refusing to launch {name}: the working tree has "
            "uncommitted changes. The worker is created from your committed "
            "HEAD, so uncommitted changes never reach it (and `mngr create` "
            "refuses a dirty tree). Commit your changes -- do NOT stash "
            "(stashed work gets lost during multi-agent coordination) -- then "
            "relaunch.",
            file=sys.stderr,
        )
        return 2

    # Refuse a taken name here rather than let ``mngr create`` fail on it: mngr
    # refuses duplicate names in any state, and a worker stopped after a
    # failure keeps its name on purpose (see ``stop``), so this is an expected
    # event with a known remedy. An unreadable listing never blocks; the create
    # surfaces its own refusal then.
    existing = _record_named(_agent_records(runner), name)
    if existing is not None:
        print(
            f"create_worker: refusing to launch {name}: "
            f"{_agent_phrase(name, existing)} already exists. Destroy it with "
            f"`create_worker.py destroy --name {name}` or pick another name.",
            file=sys.stderr,
        )
        return 2

    # Stamp the lead agent (this launcher) into the task file before creating
    # the worker, so the report has a valid return address and an unaddressable
    # case fails fast rather than after provisioning.
    lead = _ensure_lead_agent(task_file)
    if lead.exit_code is not None:
        return lead.exit_code
    # Stamp the task file's own path too, so the worker reads where its task
    # file is instead of searching for it. The same repo root relativizes the
    # ``runtime_dir`` label and the syncs below.
    toplevel = _repo_toplevel(runner)
    _ensure_task_file_path(task_file, toplevel)

    create_argv = [
        "mngr",
        "create",
        name,
        "-t",
        template,
        # Marks this as an agent-created (worker) agent so the OOM
        # agent-tagging hook puts it in the worker-agent band -- shed
        # before user-created agents (but after every agent's
        # subprocesses) under memory pressure.
        "--label",
        "agent_created=true",
    ]
    if lead.lead_name is not None:
        # The same lead the task file now names, as a label on the agent
        # itself: it makes the lead/worker edge visible in ``mngr list``, so a
        # lead can see its own workers (and, at any depth, whether one of them
        # is waiting on children of its own) without opening a task file.
        create_argv += ["--label", f"{_LEAD_AGENT_LABEL}={lead.lead_name}"]
    # Where the worker's task file and reports live, relative to the repo root,
    # so ``destroy`` can carry this worker's runtime dir up into the destroying
    # lead's tree without opening the task file.
    create_argv += [
        "--label",
        f"{_RUNTIME_DIR_LABEL}={_repo_relative_path(runtime_dir, toplevel)}",
    ]
    try:
        runner.run(create_argv, check=True)
    except subprocess.CalledProcessError as exc:
        # mngr's own refusals (a dirty tree, a failed provisioning command) are
        # printed by mngr itself; the launch reports the failure in its own
        # terms rather than as a traceback.
        print(
            f"create_worker: `mngr create {name}` failed with exit code "
            f"{exc.returncode}; no worker was created. See mngr's output above."
            f"{_leftover_record_hint(name, runner)}",
            file=sys.stderr,
        )
        return 2

    rsync_dir(name, runtime_dir, runner, toplevel)
    if artifacts_dir is not None:
        rsync_dir(name, artifacts_dir, runner, toplevel)

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

    print(f"create_worker: worker {name} launched and runtime synced")
    return 0


def _oom_priority_src() -> Path:
    """Path to the in-repo ``oom_priority`` package source.

    ``oom_priority`` is a first-party, stdlib-only package that the OOM Claude
    hooks reach by adding its ``src`` dir to ``sys.path`` (it is not a declared
    dependency anywhere); this script does the same. We locate ``src`` by
    walking up to the repo root -- the ancestor that contains
    ``system/services/oom_priority/src`` -- rather than counting a fixed number of parent
    directories, so the lookup keeps working if this script is ever relocated
    within the repo. Raises ``RuntimeError`` if it can't be found, since the
    package is always present in the repo and its absence is a real
    misconfiguration, not a condition to paper over.
    """
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / "system" / "services" / "oom_priority" / "src"
        if candidate.is_dir():
            return candidate
    raise RuntimeError(
        f"could not locate system/services/oom_priority/src above {Path(__file__).resolve()}"
        " -- the launch-task script must run from within the template repo"
    )


def _worker_has_pending_shed(worker_name: str) -> bool:
    """Whether the OOM daemon shed this worker's own agent and it is not yet
    revived, per the shed ledger.

    Resolved through the ``oom_priority`` package (the same code the kill hook
    and revival hook use), imported via a ``sys.path`` insert. The package is
    always present in the repo, so an import failure is a real misconfiguration
    and is allowed to propagate rather than being swallowed into a misleading
    "not shed" answer.
    """
    src = _oom_priority_src()
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from oom_priority.ledger import has_pending_shed

    return has_pending_shed(worker_name)


# The lifecycle state mngr reports after ``mngr stop``.
_STOPPED_STATE = "STOPPED"
# The states in which an agent has ended its turn and is doing no further work
# on its own -- what "idle" means for a worker the lead is waiting on.
_IDLE_STATES = ("WAITING", _STOPPED_STATE)
# The states in which an agent is still a live piece of work: RUNNING is
# mid-turn, WAITING has ended a turn but is still a resumable agent its own
# lead may be about to answer (a ``gate`` report round trip looks exactly like
# this). Either one, in a *child*, means its parent is not finished.
_LIVE_CHILD_STATES = ("RUNNING", "WAITING")


# Labels ``launch`` stamps on every worker it creates.
_LEAD_AGENT_LABEL = "lead_agent"
_RUNTIME_DIR_LABEL = "runtime_dir"


def _agent_records(runner: Runner) -> tuple[Mapping[str, object], ...]:
    """Every agent record from one ``mngr list --format jsonl`` call.

    One call answers both questions a poll asks -- the worker's own state and
    whether it has live children -- so the poll interval costs exactly one
    ``mngr list`` no matter how deep the dispatch tree is.

    Deliberately failure-tolerant: a query error (mngr missing, a hung listing,
    a non-zero exit) yields an empty tuple, which every caller reads as "no
    evidence", so a transient hiccup can never end a healthy await early. The
    timeout remains the backstop.
    """
    try:
        result = runner.run(
            ["mngr", "list", "--format", "jsonl", "--on-error", "continue"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if getattr(result, "returncode", 0) != 0:
        return ()
    records: list[Mapping[str, object]] = []
    for line in (getattr(result, "stdout", "") or "").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("resource_type") == "agent":
            records.append(record)
    return tuple(records)


def _record_named(
    records: Sequence[Mapping[str, object]], name: str
) -> Mapping[str, object] | None:
    """The agent record whose ``name`` is ``name``, or ``None`` if absent."""
    return next((r for r in records if r.get("name") == name), None)


def _record_field(record: Mapping[str, object], key: str) -> str | None:
    """A record's ``key`` as a non-empty string, or ``None``."""
    value = record.get(key)
    return value if isinstance(value, str) and value else None


def _record_name(record: Mapping[str, object]) -> str:
    """The record's agent name. Only for records ``_dispatch_subtree`` yielded
    or ``_record_named`` found -- both guarantee one, so a missing name here is
    a bug, not input."""
    name = _record_field(record, "name")
    assert name is not None
    return name


def _record_label(record: Mapping[str, object], key: str) -> str | None:
    """A record's ``labels.<key>`` as a non-empty string, or ``None``."""
    labels = record.get("labels")
    if not isinstance(labels, dict):
        return None
    value = labels.get(key)
    return value if isinstance(value, str) and value else None


def _agent_phrase(name: str, record: Mapping[str, object]) -> str:
    state = _record_field(record, "state") or "unknown"
    lead = _record_label(record, _LEAD_AGENT_LABEL)
    lead_text = f" (a worker of {lead})" if lead is not None else ""
    return f"an agent named {name!r} in state {state}{lead_text}"


def _leftover_record_hint(name: str, runner: Runner) -> str:
    """The sentence to append to a failed-create message when the failed
    ``mngr create`` left an agent record named ``name`` behind.

    mngr registers the agent before it provisions it, so a create that fails
    in a provisioning command (a ``uv sync`` that cannot resolve, say) leaves a
    STOPPED record that makes the *next* launch of the same name fail on the
    name alone.
    """
    record = _record_named(_agent_records(runner), name)
    if record is None:
        return ""
    return (
        f" The failed create left {_agent_phrase(name, record)} behind; run "
        f"`create_worker.py destroy --name {name}` before relaunching."
    )


def _worker_is_idle(
    worker_name: str,
    runner: Runner,
    pending_shed_check: Callable[[str], bool] = _worker_has_pending_shed,
) -> bool:
    """Whether the worker has ended its turn *and* has no sub-worker still alive.

    A worker may itself be a lead. While it waits on a sub-worker it has ended
    its own turn, so its state alone reads as idle -- and a lead that trusted
    that would abandon a perfectly healthy nested dispatch with
    ``_AWAIT_IDLE_RC``. So a worker with a live child is never idle: any agent
    labelled ``lead_agent=<worker_name>`` whose state is RUNNING or WAITING and
    which has no pending OOM shed keeps its parent counted as busy.

    The shed check is what keeps that from becoming a hang in the other
    direction: a child shed by the OOM daemon stays in a live-looking state
    forever without doing any work, so it must not hold its parent open. It is
    injected (defaulting to the real ledger reader) so tests can drive the
    distinction without writing ledger files.

    Both questions are answered from a single ``mngr list`` (see
    ``_agent_records``), and any failure to read it answers "not idle" -- the
    timeout remains the backstop.
    """
    records = _agent_records(runner)
    own = _record_named(records, worker_name)
    if own is None or _record_field(own, "state") not in _IDLE_STATES:
        return False
    for record in records:
        if _record_label(record, _LEAD_AGENT_LABEL) != worker_name:
            continue
        if _record_field(record, "state") not in _LIVE_CHILD_STATES:
            continue
        child_name = _record_field(record, "name")
        if child_name is None or not pending_shed_check(child_name):
            # A child with no name is still a child: treat it as live rather
            # than declaring its parent finished on an unreadable record.
            return False
    return True


class ReportResult(NamedTuple):
    """Structured view of a worker's terminal/gate report.

    ``report_type`` and ``name`` are the frontmatter ``type``/``name`` fields, or
    ``None`` when the report has no parseable frontmatter (caller treats that as a
    failure). ``body`` is the prose below the frontmatter; ``raw`` is the verbatim
    report text.
    """

    report_type: str | None
    name: str | None
    body: str
    raw: str


def parse_report(text: str) -> ReportResult:
    """Parse a worker report's YAML frontmatter (``type``/``name``) and body.

    Deliberately tolerant -- unlike ``_read_frontmatter_field``, which strictly
    raises on a malformed *task file* (a lead-authored, deterministic input
    where bad YAML is an authoring bug). A report is *agent-authored runtime
    output*: an unparseable one yields ``report_type=None``/``name=None`` with
    the whole text preserved as the body, so the caller (``launch_sync``) surfaces
    the raw report as structured data for the calling process to handle, rather
    than crashing the collection path and losing the worker's output. No data is
    discarded -- ``raw`` always holds the verbatim text.
    """
    try:
        frontmatter, body = _split_frontmatter(text)
    except yaml.YAMLError:
        return ReportResult(None, None, text, text)
    if frontmatter is None:
        # No parseable frontmatter: preserve the whole text as the body so no
        # data is discarded (and the caller can still surface the raw report).
        return ReportResult(None, None, text, text)
    report_type = frontmatter.get("type")
    name = frontmatter.get("name")
    return ReportResult(
        report_type=report_type if isinstance(report_type, str) else None,
        name=name if isinstance(name, str) else None,
        body=body,
        raw=text,
    )


# Sortable, filename-safe UTC stamp: the archive of a gate cycle reads in the
# order the reports arrived.
_ARCHIVE_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
# Stand-in for a ``type``/``name`` the report did not carry, so an unparseable
# report still archives under a name that says what happened.
_UNPARSED_FIELD = "unparsed"


def _utc_timestamp() -> str:
    """The current UTC time as ``YYYYmmddTHHMMSSZ``."""
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        _ARCHIVE_TIMESTAMP_FORMAT
    )


def _consumed_report_name(report: ReportResult, timestamp: str, suffix: str) -> str:
    """The archive filename for a collected report: ``<stamp>-<type>-<name><ext>``.

    The kind is in the name because a gate cycle archives several reports into
    one directory: a lead reading ``consumed/`` should see *what* each report
    was without opening it.
    """
    report_type = report.report_type or _UNPARSED_FIELD
    name = report.name or _UNPARSED_FIELD
    return f"{timestamp}-{report_type}-{name}{suffix}"


def _consumed_dir(report_path: Path) -> Path:
    """Where handled reports and milestones are archived, beside the report.

    The one place the prose's "move it aside" remedy and the code's
    already-handled checks both resolve to.
    """
    return report_path.parent / "consumed"


def _milestones_dir(report_path: Path) -> Path:
    """Where a worker drops milestone reports, one file per milestone.

    They cannot share ``report.md``'s single slot: a milestone does not stop the
    worker, so it may reach a gate before the lead has consumed the milestone,
    and the gate would overwrite it.
    """
    return report_path.parent / "milestones"


def _unconsumed_milestones(report_path: Path) -> list[Path]:
    """Milestone files with no same-named entry in ``consumed/``, oldest first.

    The worker keeps its copies, so every later push re-delivers them; matching
    on the basename (which carries the commit's short sha) makes a re-delivered
    file inert while the same name at a new commit is a new event. Sorted by
    mtime, then name, so declaration order is stable.
    """
    milestones_dir = _milestones_dir(report_path)
    if not milestones_dir.is_dir():
        return []
    consumed_dir = _consumed_dir(report_path)
    unconsumed = [
        path
        for path in milestones_dir.glob("*.md")
        if path.is_file() and not (consumed_dir / path.name).exists()
    ]
    return sorted(unconsumed, key=lambda path: (path.stat().st_mtime, path.name))


def _archive_milestones(report_path: Path) -> None:
    """Move every unconsumed milestone into ``consumed/`` under its own name.

    ``launch_sync`` ignores milestones while it waits, but ``launch``'s guard
    refuses to start over one, so a milestone declared mid-run would trap the
    next ``launch_sync`` on the same task file just as the report would. Once
    the terminal report is in hand they are moot. Basenames are unique (short
    sha), so no disambiguation is needed.
    """
    for milestone_path in _unconsumed_milestones(report_path):
        _archive_report(milestone_path, archive_dir=_consumed_dir(report_path))


def _archive_report(
    report_path: Path, target_name: str | None = None, archive_dir: Path | None = None
) -> None:
    """Move a collected report out of ``finish_report_path`` into ``consumed/``.

    ``launch``'s stale-report guard refuses to start while anything sits at
    ``finish_report_path``, and neither ``destroy`` nor the worker touches this
    file once delivered (it lives in the lead's runtime dir), so without this a
    lead's next dispatch on the same task file -- a relaunch after a gate, or a
    service's repeated ``launch_sync`` -- would be blocked by the report it just
    read. We move it aside rather than delete it: the raw report is worth
    keeping, and ``consumed/`` is the same archive the prose has always named.

    ``target_name`` is the name to file it under (default: the report's own
    filename). ``archive_dir`` is the ``consumed/`` directory to file it in
    (default: the one beside the report; a milestone passes the report's, since
    it lives one directory down). Collisions -- two reports in the same second
    -- are disambiguated with a numeric suffix, so successive reports each keep
    their own copy and nothing is overwritten. A no-op if the file is already
    gone (e.g. a race with an external cleanup).
    """
    if not report_path.exists():
        return
    consumed_dir = _consumed_dir(report_path) if archive_dir is None else archive_dir
    consumed_dir.mkdir(parents=True, exist_ok=True)
    filename = target_name if target_name is not None else report_path.name
    target = consumed_dir / filename
    stem, suffix = Path(filename).stem, Path(filename).suffix
    index = 1
    while target.exists():
        target = consumed_dir / f"{stem}.{index}{suffix}"
        index += 1
    report_path.replace(target)


def await_report(
    report_path: Path,
    timeout_seconds: float,
    poll_interval_seconds: float,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out: TextIO | None = None,
    worker_name: str | None = None,
    pending_shed_check: Callable[[str], bool] | None = None,
    idle_check: Callable[[str], bool] | None = None,
    archive_timestamp: Callable[[], str] = _utc_timestamp,
    watch_milestones: bool = True,
) -> int:
    """Block until ``report_path`` exists, print its contents, and consume it.

    Returns 0 after writing the report contents to ``out`` (default stdout);
    returns ``_AWAIT_TIMEOUT_RC`` if the deadline passes first, leaving a note
    on stderr so the caller diagnoses worker liveness per lead-proxy.md rather
    than treating the timeout as a terminal failure.

    A collected report is *consumed*, not just printed: it is moved into
    ``<dir>/consumed/<stamp>-<type>-<name>.md`` (see ``_archive_report``). Every
    await hands its caller the full report on stdout, so the file itself has
    done its job -- and leaving it at ``finish_report_path`` would block the
    lead's next ``launch`` on the same task file, which is the ordinary shape of
    a gate cycle. ``archive_timestamp`` is injected so tests can pin the name
    without real time.

    With ``watch_milestones`` (the default), an unconsumed milestone under
    ``<dir>/milestones/`` ends the poll the same way -- contents to ``out``,
    path to stderr, return 0 -- so the caller's parse-and-branch-on-``type``
    loop absorbs it with no new exit code. ``report.md`` is checked first and
    wins; the milestone is still there for the next poll. A returned milestone
    is archived like a report, but under its *own* filename: the worker keeps
    its copy and every later push re-delivers it, and ``_unconsumed_milestones``
    recognises a handled one by that basename in ``consumed/``. Merging it, or
    deferring it, is lead judgment (lead-proxy.md); the sha in the archived
    name is what a deferred merge later needs.

    If ``worker_name`` and ``pending_shed_check`` are supplied, each poll also
    checks whether the worker's own agent was shed by the OOM daemon. A shed
    worker will never report until it is revived, so rather than wait out the
    full timeout we surface an actionable message and return ``_AWAIT_SHED_RC``.
    The report file is still checked first each loop, so a report that landed
    before the shed (or a worker revived and reporting) still wins.

    If ``worker_name`` and ``idle_check`` are supplied, each poll also checks
    whether the worker's agent has ended its turn. A worker observed idle for
    ``_IDLE_POLLS_BEFORE_GIVING_UP`` consecutive polls with no report is either
    finished-but-undelivered (its report likely sits in its own worktree) or
    stalled -- both deserve an immediate, actionable ``_AWAIT_IDLE_RC`` rather
    than the remainder of the timeout in silence. The shed check runs first:
    a shed agent also reads as not-running, and the shed diagnosis is the more
    specific (and differently-recovered) one. ``_worker_is_idle`` -- the check
    the CLI wires in -- counts a worker that is waiting on a live sub-worker of
    its own as busy, so a nested dispatch is never mistaken for a stalled one.

    ``sleeper``/``clock`` are injected so tests can drive the poll loop without
    real time. The file is checked before the first sleep, so a report already
    present returns immediately.
    """
    stream: TextIO = sys.stdout if out is None else out
    deadline = clock() + timeout_seconds
    consecutive_idle_count = 0
    while True:
        if report_path.is_file():
            text = report_path.read_text(encoding="utf-8")
            stream.write(text)
            _archive_report(
                report_path,
                _consumed_report_name(
                    parse_report(text), archive_timestamp(), report_path.suffix
                ),
            )
            return 0
        if watch_milestones:
            unconsumed = _unconsumed_milestones(report_path)
            if unconsumed:
                milestone_path = unconsumed[0]
                stream.write(milestone_path.read_text(encoding="utf-8"))
                consumed_dir = _consumed_dir(report_path)
                _archive_report(milestone_path, archive_dir=consumed_dir)
                print(
                    f"create_worker: milestone report from {milestone_path}, "
                    f"archived as {consumed_dir / milestone_path.name}",
                    file=sys.stderr,
                )
                return 0
        if (
            worker_name is not None
            and pending_shed_check is not None
            and pending_shed_check(worker_name)
        ):
            print(
                f"create_worker: worker '{worker_name}' was stopped by the OOM "
                "daemon to relieve memory pressure -- its agent process was shed "
                "and its background tasks (including its own report poll) were "
                "cancelled, so it will NOT report until it is revived. Revive it "
                f"with: mngr start {worker_name} --restart  (a plain `mngr message` "
                "or `mngr start` will not relaunch a shed agent), then nudge it to "
                f"continue (mngr message {worker_name} -m continue). You do not "
                "need to resend the task -- it survives in the worker's "
                "conversation history, and a SessionStart hook already tells the "
                "revived worker it was paused, so it re-checks state before "
                "continuing.",
                file=sys.stderr,
            )
            return _AWAIT_SHED_RC
        if worker_name is not None and idle_check is not None:
            consecutive_idle_count = (
                consecutive_idle_count + 1 if idle_check(worker_name) else 0
            )
            if consecutive_idle_count >= _IDLE_POLLS_BEFORE_GIVING_UP:
                print(
                    f"create_worker: worker '{worker_name}' has ended its turn "
                    f"(idle for {consecutive_idle_count} consecutive polls) but no "
                    f"report has appeared at {report_path}. A worker still waiting "
                    "on a live sub-worker of its own does NOT count as idle, so "
                    "this is not a nested dispatch in flight. Either it finished "
                    "and the report delivery failed (look for the report inside the "
                    "worker's own worktree -- its `work_dir` in `mngr list "
                    f"--format jsonl` for {worker_name} -- under the report's "
                    "relative path, and copy it to the path above), or it stopped "
                    "without reporting (read its transcript: "
                    f"mngr transcript {worker_name}; nudge it with mngr message "
                    f"{worker_name} -m 'deliver your report per "
                    "worker-reporting.md'). Not waiting out the remaining timeout.",
                    file=sys.stderr,
                )
                return _AWAIT_IDLE_RC
        if clock() >= deadline:
            print(
                f"create_worker: timed out after {timeout_seconds:g}s waiting for "
                f"{report_path}; the worker may still be alive -- diagnose liveness "
                f"per lead-proxy.md before invoking the failure flow",
                file=sys.stderr,
            )
            return _AWAIT_TIMEOUT_RC
        sleeper(poll_interval_seconds)


def _dispatch_subtree(
    root_name: str, records: Sequence[Mapping[str, object]]
) -> tuple[Mapping[str, object], ...]:
    """Every agent whose ``lead_agent`` chain reaches ``root_name``, in
    post-order (each agent after all of its own workers), excluding the root.

    The order is what lets a teardown work deepest-first: a descendant is
    handled while its lead -- whose worktree holds the descendant's runtime dir
    -- still exists. Records without a ``name`` are skipped, and a mislabelled
    cycle cannot loop it.
    """
    children: dict[str, list[Mapping[str, object]]] = {}
    for record in records:
        lead = _record_label(record, _LEAD_AGENT_LABEL)
        if lead is not None and _record_field(record, "name") is not None:
            children.setdefault(lead, []).append(record)
    ordered: list[Mapping[str, object]] = []
    visited = {root_name}

    def _visit(lead: str) -> None:
        for child in children.get(lead, ()):
            child_name = _record_name(child)
            if child_name in visited:
                continue
            visited.add(child_name)
            _visit(child_name)
            ordered.append(child)

    _visit(root_name)
    return tuple(ordered)


# Where every flow keeps its runtime dirs (``data/.tasks/<flow>/<slug>/``, per
# AGENTS.md).
_TASKS_DIR = Path("data/.tasks")


def _relocate_task_dirs(
    record: Mapping[str, object], runner: Runner, toplevel: Path | None
) -> bool:
    """Pull the runtime dirs of the workers ``record``'s agent dispatched out
    of its worktree into the caller's tree; returns whether it landed.

    A lead creates each of its workers' runtime dirs (task file, ``consumed/``
    reports, milestones) in its *own* worktree, which a destroy is about to
    remove -- and a lead that has already destroyed a merged sub-worker holds
    the only copy of that sub-worker's runtime dir, with no record left to
    find it by. So the pull is by convention rather than by label: the whole
    ``data/.tasks/`` tree, minus the agent's own runtime dir (the ``runtime_dir``
    label ``launch`` stamped), which the caller already has -- and whose
    worker-side copy carries the worker's own ``report.md``, which back in the
    caller's tree would trip the next launch's stale-report guard. Without the
    label (an older launcher) the agent's own dir cannot be excluded, so the
    pull is skipped.
    """
    name = _record_name(record)
    runtime_dir = _record_label(record, _RUNTIME_DIR_LABEL)
    if runtime_dir is None:
        reason = "it carries no `runtime_dir` label (launched by an older launcher)"
    else:
        excludes: list[str] = []
        own = Path(runtime_dir)
        if own.is_relative_to(_TASKS_DIR):
            excludes.append(f"/{own.relative_to(_TASKS_DIR).as_posix()}")
        if rsync_dir_from(name, _TASKS_DIR, runner, toplevel, excludes):
            return True
        reason = f"`mngr rsync` from {name} failed"
    print(
        f"create_worker: warning: could not relocate the runtime dirs of {name}'s "
        f"workers ({reason}); destroying it anyway.",
        file=sys.stderr,
    )
    return False


def _unmerged_work_warning(record: Mapping[str, object], runner: Runner) -> None:
    """Print one line on what destroying ``record``'s agent leaves unmerged.

    Uncommitted changes are lost with the worktree (dead-worker-recovery.md's
    salvage flow is for that); commits on ``mngr/<name>`` not in HEAD stay on
    the branch. Never blocks: a lead destroys after it has merged, and a
    superseded pass is meant to be dropped.
    """
    name = _record_name(record)
    work_dir = _record_field(record, "work_dir")
    if work_dir is None:
        print(
            f"create_worker: {name}: no work_dir in its record; skipping the "
            "unmerged-work check",
            file=sys.stderr,
        )
        return
    status = _git_output(["git", "-C", work_dir, "status", "--porcelain"], runner)
    if status is None:
        dirty_text = "worktree state unknown"
    elif status:
        dirty_text = "worktree has UNCOMMITTED changes (lost with the worktree)"
    else:
        dirty_text = "worktree clean"
    branch = f"mngr/{name}"
    count = _git_output(["git", "rev-list", "--count", f"HEAD..{branch}"], runner)
    if count is None:
        commits_text = f"branch {branch} not found"
    else:
        commits_text = f"{count or '0'} commit(s) on {branch} not in HEAD"
    print(f"create_worker: {name}: {dirty_text}; {commits_text}", file=sys.stderr)


class _LifecycleOutcome(NamedTuple):
    """What happened to one agent during a ``destroy`` or ``stop``."""

    name: str
    action: str
    succeeded: bool
    detail: str


def _print_outcomes(outcomes: Sequence[_LifecycleOutcome]) -> int:
    """Write one line per agent to stderr; ``0`` if all succeeded, else ``1``.

    A partial failure is reported, not hidden: the caller sees which agent is
    still there and why, and the non-zero exit keeps a scripted caller from
    believing the subtree is gone.
    """
    for outcome in outcomes:
        verdict = outcome.action if outcome.succeeded else f"NOT {outcome.action}"
        print(
            f"create_worker: {outcome.name}: {verdict} ({outcome.detail})",
            file=sys.stderr,
        )
    return 0 if all(o.succeeded for o in outcomes) else 1


def _mngr_lifecycle_call(
    subcommand: str, name: str, flags: Sequence[str], action: str, runner: Runner
) -> _LifecycleOutcome:
    argv = ["mngr", subcommand, name, *flags]
    command = " ".join(argv)
    try:
        result = runner.run(argv, check=False, timeout=_MNGR_LIFECYCLE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return _LifecycleOutcome(name, action, False, f"`{command}` timed out")
    returncode = getattr(result, "returncode", 0)
    if returncode == 0:
        return _LifecycleOutcome(name, action, True, command)
    return _LifecycleOutcome(name, action, False, f"`{command}` exited {returncode}")


def _orphan_warning(
    name: str, subtree: Sequence[Mapping[str, object]], action: str
) -> None:
    """Name each descendant a ``--no-recursive`` teardown leaves behind."""
    if not subtree:
        return
    listed = ", ".join(
        f"{_record_name(r)} ({_record_field(r, 'state') or 'unknown'})" for r in subtree
    )
    print(
        f"create_worker: warning: {name} {action} without its {len(subtree)} "
        f"descendant worker(s): {listed}. Nothing will clean them up; destroy "
        "each with `create_worker.py destroy --name <name>`.",
        file=sys.stderr,
    )


def destroy(
    name: str,
    runner: Runner | None = None,
    recursive: bool = True,
    delete_branches: bool = False,
) -> int:
    """Destroy the worker agent and (by default) every worker under it.

    Deepest first, the root last, so each agent's worktree is still there when
    the runtime dirs of the workers it dispatched are pulled out of it.
    RUNNING descendants are destroyed too -- a superseded pass's in-flight
    siblings are exactly what needs to go. Branches ``mngr/<name>`` survive
    unless ``delete_branches`` adds ``-b``. Every step is best-effort; one
    outcome line per agent, and ``0`` only if every agent was destroyed.
    """
    runner = runner or Runner()
    records = _agent_records(runner)
    subtree = _dispatch_subtree(name, records)
    toplevel = _repo_toplevel(runner)
    flags = ["--force", "-b"] if delete_branches else ["--force"]
    outcomes: list[_LifecycleOutcome] = []
    if not recursive:
        _orphan_warning(name, subtree, "destroyed")
        subtree = ()
    for record in subtree:
        _relocate_task_dirs(record, runner, toplevel)
        _unmerged_work_warning(record, runner)
        child = _record_name(record)
        outcomes.append(
            _mngr_lifecycle_call("destroy", child, flags, "destroyed", runner)
        )
    own = _record_named(records, name)
    if own is not None:
        _relocate_task_dirs(own, runner, toplevel)
        _unmerged_work_warning(own, runner)
    outcomes.append(_mngr_lifecycle_call("destroy", name, flags, "destroyed", runner))
    return _print_outcomes(outcomes)


def stop(name: str, runner: Runner | None = None, recursive: bool = True) -> int:
    """Stop the worker agent and (by default) every worker under it.

    ``mngr stop <agent> --archive`` per agent, children first and the root
    last: processes go, worktrees and branches stay, and the ``archived_at``
    label marks each one as stopped by its lead on purpose -- which is how
    dead-worker-recovery.md tells a deliberately stopped worker from a crashed
    one whose restart is worth trying. Nothing is relocated: a stopped
    subtree's worktrees are still there to read.
    """
    runner = runner or Runner()
    subtree = _dispatch_subtree(name, _agent_records(runner))
    outcomes: list[_LifecycleOutcome] = []
    if not recursive:
        _orphan_warning(name, subtree, "stopped")
        subtree = ()
    for record in subtree:
        child = _record_name(record)
        outcomes.append(
            _mngr_lifecycle_call("stop", child, ["--archive"], "stopped", runner)
        )
    outcomes.append(
        _mngr_lifecycle_call("stop", name, ["--archive"], "stopped", runner)
    )
    return _print_outcomes(outcomes)


# Exit code for a report that could not be delivered at all: neither pushed to
# the lead nor resolved to the lead's work dir through ``mngr list``. Shares
# ``launch``'s "unusable inputs" code -- from the caller's side both mean the
# command did nothing useful and needs a human.
_REPORT_UNDELIVERABLE_RC = 2


def _report_undeliverable(report_path: Path, reason: str) -> int:
    """Report a delivery failure on stderr and return the failure exit code."""
    print(
        f"create_worker: the report was written to {report_path} in this "
        f"worker's own tree but could NOT be delivered to the lead: {reason}. "
        "From the lead's side an undelivered report is indistinguishable from a "
        "hung worker, so this is a hard failure: deliver it by hand (copy it to "
        "the same relative path inside the lead's work dir) or fix the missing "
        "piece and re-run.",
        file=sys.stderr,
    )
    return _REPORT_UNDELIVERABLE_RC


def _deliver_report_through_listing(
    report_text: str,
    report_path: Path,
    worker_name: str | None,
    runner: Runner,
) -> int:
    """Copy the report into the lead's work dir, resolved through ``mngr list``.

    The rsync push is the normal delivery; this is the fallback for when it
    cannot be attempted (the task file names no ``lead_agent``) or it failed.
    Two lookups in one listing give a real destination on disk without either
    agent's cooperation: the worker's own agent record carries the
    ``lead_agent`` label ``launch`` attached at create time, and that lead's
    record carries its ``work_dir``. Because ``finish_report_path`` is
    repo-relative, joining it onto the lead's work dir is the same file the
    lead's ``await`` is polling for.

    Every way this can fail names the piece that was missing and returns
    ``_REPORT_UNDELIVERABLE_RC``.
    """
    if not worker_name:
        return _report_undeliverable(
            report_path,
            "this worker's own name is unknown (MNGR_AGENT_NAME is unset and "
            "--worker-name was not passed), so there is nothing to look its "
            "lead up by",
        )
    records = _agent_records(runner)
    own_record = _record_named(records, worker_name)
    if own_record is None:
        return _report_undeliverable(
            report_path, f"`mngr list` has no agent record named {worker_name!r}"
        )
    lead_name = _record_label(own_record, _LEAD_AGENT_LABEL)
    if lead_name is None:
        return _report_undeliverable(
            report_path,
            f"the agent record for {worker_name!r} carries no `lead_agent` "
            "label, so its lead is unknown",
        )
    lead_record = _record_named(records, lead_name)
    if lead_record is None:
        return _report_undeliverable(
            report_path,
            f"`mngr list` has no agent record named {lead_name!r}, the lead "
            f"{worker_name!r} is labelled with",
        )
    work_dir = _record_field(lead_record, "work_dir")
    if work_dir is None:
        return _report_undeliverable(
            report_path, f"the agent record for {lead_name!r} carries no `work_dir`"
        )
    if report_path.is_absolute():
        # Joining an absolute path onto the work dir yields the path itself, so
        # this would "deliver" the report to the copy already in the worker's own
        # tree and report success -- exactly the silent non-delivery this whole
        # path exists to prevent.
        return _report_undeliverable(
            report_path,
            "`finish_report_path` is absolute, so it cannot be resolved against "
            f"the lead's work dir ({work_dir}); this delivery needs the "
            "repo-relative path both sides share",
        )
    destination = Path(work_dir) / report_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(report_text, encoding="utf-8")
    print(str(destination))
    return 0


def report_to_lead(
    task_file: Path,
    report_type: str,
    name: str,
    body_file: Path,
    runner: Runner | None = None,
    worker_name: str | None = None,
) -> int:
    """Write this worker's report and deliver it to its lead. Returns an exit code.

    The worker side of the dispatch contract, and the exact mirror of what
    ``await`` waits for. The report is written at the task file's
    ``finish_report_path`` *within the worker's own tree* -- that path is
    repo-relative, so the same string names the file on both sides -- carrying
    the ``type``/``name`` frontmatter ``parse_report`` reads. Delivery then
    pushes the report's parent directory to the lead with ``rsync_dir``'s
    conventions (``mngr rsync`` moves directories, not single files), so the
    file lands at the lead's ``finish_report_path`` and its poll returns.

    The push is the ready signal, so it only ever runs on a fully written
    report. When it cannot run (no ``lead_agent`` in the task file) or fails,
    delivery falls back to resolving the lead's work dir from ``mngr list`` and
    copying the file there. When neither works the command fails loudly with
    ``_REPORT_UNDELIVERABLE_RC``: from the lead's side, a worker that finished
    but could not say so is indistinguishable from a hung one, so a silent
    non-delivery is the one outcome this must never produce.

    ``worker_name`` (the worker's own ``MNGR_AGENT_NAME``) is only needed by the
    fallback, which looks the worker up to read its ``lead_agent`` label.
    """
    runner = runner or Runner()
    if not body_file.is_file():
        print(f"create_worker: --body-file not found: {body_file}", file=sys.stderr)
        return _REPORT_UNDELIVERABLE_RC
    # A missing/malformed ``finish_report_path`` is an authoring bug in the task
    # file, so let it raise with a full traceback (as ``await`` does) rather than
    # degrading into a terse message about a file the worker cannot fix.
    report_path = _read_finish_report_path(task_file)
    lead_agent = _read_frontmatter_field(task_file, "lead_agent")

    body = body_file.read_text(encoding="utf-8").strip("\n")
    report_text = f"---\ntype: {report_type}\nname: {name}\n---\n\n{body}\n"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")

    if lead_agent is None:
        print(
            "create_worker: the task file names no `lead_agent`, so there is no "
            "address to push the report to; resolving the lead through `mngr "
            "list` instead.",
            file=sys.stderr,
        )
    else:
        try:
            rsync_dir(lead_agent, report_path.parent, runner)
        except subprocess.CalledProcessError as exc:
            print(
                f"create_worker: pushing the report to {lead_agent} failed with "
                f"exit code {exc.returncode}; resolving the lead's work dir "
                "through `mngr list` instead.",
                file=sys.stderr,
            )
        else:
            print(f"{lead_agent}:{_normalize_dir(str(report_path.parent))}")
            return 0
    return _deliver_report_through_listing(
        report_text, report_path, worker_name, runner
    )


def _emit_run_result(
    payload: Mapping[str, object], stream: TextIO, result_path: Path | None
) -> None:
    """Write the run-result JSON to stdout and, if given, to a dedicated file.

    The file is the machine contract for programmatic callers: they read the exact
    payload from a path they chose, rather than guessing which stdout line is the
    result. Stdout still carries the JSON for humans and shell callers.
    """
    line = json.dumps(payload)
    stream.write(line + "\n")
    if result_path is not None:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(line, encoding="utf-8")


def launch_sync(
    name: str,
    template: str,
    runtime_dir: Path,
    task_file: Path,
    timeout_seconds: float,
    poll_interval_seconds: float,
    destroy_on_finish: bool = True,
    state_dir: Path | None = None,
    runner: Runner | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out: TextIO | None = None,
    result_path: Path | None = None,
    archive_timestamp: Callable[[], str] = _utc_timestamp,
) -> int:
    """Launch a worker, wait for its report in the *foreground*, emit JSON, destroy.

    The blocking path for non-interactive callers (services). Returns the
    launch exit code if launch fails, the await timeout code if the report never
    appears (without destroying -- the report may still be coming), or 0 once a
    report is collected. Writes a single JSON object to ``out`` (default stdout)
    describing the outcome: ``timed_out`` plus the report ``type``/``name``/``body``
    and the worker ``branch``. When ``result_path`` is set, the same JSON is also
    written there as the machine-readable contract for programmatic callers.
    Milestones are not watched: callers want one terminal result.
    """
    runner = runner or Runner()
    stream: TextIO = sys.stdout if out is None else out

    # Resolve the wait target *before* creating the worker: a missing/malformed
    # ``finish_report_path`` is an authoring bug, and reading it up front keeps it
    # from half-creating a worker (matching ``launch``'s preflight contract). The
    # field comes from the task file's frontmatter, which already exists, so this
    # is safe to read this early.
    report_path = _read_finish_report_path(task_file)

    launch_rc = launch(
        name=name,
        template=template,
        runtime_dir=runtime_dir,
        task_file=task_file,
        state_dir=state_dir,
        runner=runner,
    )
    if launch_rc != 0:
        return launch_rc

    buffer = io.StringIO()
    await_rc = await_report(
        report_path=report_path,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        sleeper=sleeper,
        clock=clock,
        out=buffer,
        worker_name=name,
        pending_shed_check=_worker_has_pending_shed,
        idle_check=functools.partial(_worker_is_idle, runner=runner),
        archive_timestamp=archive_timestamp,
        # A milestone returned here would be emitted as *the* run result and the
        # still-hardening worker destroyed.
        watch_milestones=False,
    )
    branch = f"mngr/{name}"
    if await_rc != 0:
        # Timed out: leave the worker alive for liveness diagnosis.
        _emit_run_result(
            {
                "timed_out": True,
                "type": None,
                "name": None,
                "body": "",
                "branch": branch,
                "raw_report": "",
            },
            stream,
            result_path,
        )
        return await_rc

    report = parse_report(buffer.getvalue())
    # The report has already been consumed into ``consumed/`` by ``await_report``
    # -- ``launch`` refuses to start if anything sits at ``finish_report_path``,
    # and ``destroy`` only removes the worker's agent/worktree, not this report,
    # which lives in the caller's runtime dir. Without that archive the next
    # ``launch_sync`` on the same task file (the fixed-path pattern services use)
    # would be trapped by its own previous report. Milestones are archived for
    # the same reason.
    _archive_milestones(report_path)
    # A failed destroy is reported alongside the result, not instead of it:
    # the caller has the report either way.
    destroy_failed = destroy_on_finish and destroy(name, runner) != 0
    _emit_run_result(
        {
            "timed_out": False,
            "type": report.report_type,
            "name": report.name,
            "body": report.body,
            "branch": branch,
            "raw_report": report.raw,
            "destroy_failed": destroy_failed,
        },
        stream,
        result_path,
    )
    return 1 if destroy_failed else 0


def _run_launch(args: argparse.Namespace, runner: Runner | None) -> int:
    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else None
    return launch(
        name=args.name,
        template=args.template,
        runtime_dir=args.runtime_dir,
        task_file=args.task_file,
        state_dir=state_dir,
        runner=runner,
    )


def _run_await(args: argparse.Namespace) -> int:
    # A missing/malformed ``finish_report_path`` is an authoring bug in the task
    # file; let the ValueError raise for a full traceback rather than swallowing
    # it into a terse exit-2 message (matches ``launch``'s handling above).
    report_path = _read_finish_report_path(args.task_file)
    # Watch the shed ledger so a worker paused for memory pressure surfaces
    # promptly (and actionably) instead of as a silent 30-minute timeout. The
    # worker name is the same ``--name`` the caller passed to ``launch``, so it
    # is a required argument here rather than something re-derived from the task
    # file or the directory layout.
    return await_report(
        report_path=report_path,
        timeout_seconds=args.timeout,
        poll_interval_seconds=args.poll_interval,
        worker_name=args.name,
        pending_shed_check=_worker_has_pending_shed,
        idle_check=functools.partial(_worker_is_idle, runner=Runner()),
    )


def _run_launch_sync(args: argparse.Namespace, runner: Runner | None) -> int:
    # Validate the wait target up front so a missing/malformed field fails like
    # await -- a ValueError here is an authoring bug, so let it raise with a full
    # traceback rather than swallowing it.
    _read_finish_report_path(args.task_file)
    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else None
    return launch_sync(
        name=args.name,
        template=args.template,
        runtime_dir=args.runtime_dir,
        task_file=args.task_file,
        timeout_seconds=args.timeout,
        poll_interval_seconds=args.poll_interval,
        destroy_on_finish=not args.keep_agent,
        state_dir=state_dir,
        runner=runner,
        result_path=args.result_json,
    )


def _run_report(args: argparse.Namespace, runner: Runner | None) -> int:
    # The worker's own name comes from the environment mngr already gives it, so
    # the prose never has to interpolate it; ``--worker-name`` exists for the
    # cases where there is no such environment (a manual run, or a test).
    return report_to_lead(
        task_file=args.task_file,
        report_type=args.type,
        name=args.name,
        body_file=args.body_file,
        runner=runner,
        worker_name=args.worker_name or os.environ.get("MNGR_AGENT_NAME"),
    )


def _run_destroy(args: argparse.Namespace, runner: Runner | None) -> int:
    return destroy(
        args.name,
        runner,
        recursive=not args.no_recursive,
        delete_branches=args.delete_branches,
    )


def _run_stop(args: argparse.Namespace, runner: Runner | None) -> int:
    return stop(args.name, runner, recursive=not args.no_recursive)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    Separate from ``main`` so the skill prose that invokes this script can be
    checked against the real argument surface (subcommands and flags) without
    running a command.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    launch_parser = subparsers.add_parser(
        "launch", help="Create the worker and hand it the task (synchronous)."
    )
    launch_parser.add_argument(
        "--name", required=True, help="Worker name; becomes the mngr/<name> branch."
    )
    launch_parser.add_argument(
        "--template",
        required=True,
        help="mngr create role template (e.g. 'worker').",
    )
    launch_parser.add_argument(
        "--runtime-dir",
        required=True,
        type=Path,
        help="Existing runtime directory synced verbatim into the worker's worktree.",
    )
    launch_parser.add_argument(
        "--task-file",
        required=True,
        type=Path,
        help="Markdown task file (must already exist; typically inside --runtime-dir).",
    )

    await_parser = subparsers.add_parser(
        "await",
        help="Block until the worker's report file appears (or an unconsumed "
        "milestone lands beside it), then print it. "
        "Run in the background, then end the turn -- the completion wakes you. "
        "Re-invoke once per gate cycle.",
    )
    await_parser.add_argument(
        "--task-file",
        required=True,
        type=Path,
        help="Same task file as launch; its frontmatter `finish_report_path` "
        "names the file to wait for.",
    )
    await_parser.add_argument(
        "--name",
        required=True,
        help="Worker name (the same one passed to launch). Used to watch the "
        "shed ledger so a worker paused for memory pressure is surfaced promptly "
        "(and actionably) instead of as a silent timeout.",
    )
    await_parser.add_argument(
        "--timeout",
        default=_DEFAULT_TIMEOUT,
        type=_parse_duration,
        help=f"Max wait before giving up (default {_DEFAULT_TIMEOUT}). "
        "Accepts e.g. '30m', '90s', '1h', or bare seconds.",
    )
    await_parser.add_argument(
        "--poll-interval",
        default=_DEFAULT_POLL_INTERVAL,
        type=_parse_duration,
        help=f"How often to check for the report (default {_DEFAULT_POLL_INTERVAL}).",
    )

    launch_sync_parser = subparsers.add_parser(
        "launch-sync",
        help="Blocking launch + foreground await + structured-result JSON + "
        "destroy, in one call. For non-interactive callers (services).",
    )
    launch_sync_parser.add_argument(
        "--name", required=True, help="Worker name; becomes the mngr/<name> branch."
    )
    launch_sync_parser.add_argument(
        "--template",
        required=True,
        help="mngr create role template (e.g. 'worker').",
    )
    launch_sync_parser.add_argument(
        "--runtime-dir",
        required=True,
        type=Path,
        help="Existing runtime directory synced verbatim into the worker's worktree.",
    )
    launch_sync_parser.add_argument(
        "--task-file",
        required=True,
        type=Path,
        help="Markdown task file; its frontmatter `finish_report_path` names the "
        "report to wait for.",
    )
    launch_sync_parser.add_argument(
        "--timeout",
        default=_DEFAULT_TIMEOUT,
        type=_parse_duration,
        help=f"Max wait for the report (default {_DEFAULT_TIMEOUT}).",
    )
    launch_sync_parser.add_argument(
        "--poll-interval",
        default=_DEFAULT_POLL_INTERVAL,
        type=_parse_duration,
        help=f"How often to check for the report (default {_DEFAULT_POLL_INTERVAL}).",
    )
    launch_sync_parser.add_argument(
        "--keep-agent",
        action="store_true",
        help="Do not destroy the worker after a report is collected "
        "(default: destroy). A timeout never destroys regardless.",
    )
    launch_sync_parser.add_argument(
        "--result-json",
        type=Path,
        default=None,
        help="Also write the result JSON to this path (the machine-readable "
        "contract for programmatic callers; stdout still carries it too).",
    )

    report_parser = subparsers.add_parser(
        "report",
        help="Worker side: write this run's report and deliver it to the lead "
        "(push, with a `mngr list` fallback). Fails loudly if neither lands.",
    )
    report_parser.add_argument(
        "--task-file",
        required=True,
        type=Path,
        help="This worker's own task file (the `task_file` path stamped in the "
        "frontmatter it was sent); its `finish_report_path` says where the "
        "report goes and its `lead_agent` who to send it to.",
    )
    report_parser.add_argument(
        "--type",
        required=True,
        help="Report kind: `gate` (mid-flight, the lead answers and you resume) "
        "or `status` (terminal).",
    )
    report_parser.add_argument(
        "--name",
        required=True,
        help="Report name -- the flow-specific marker the lead dispatches on "
        "(e.g. `done`, `stuck`, `question`). See the worker's SKILL.md for the "
        "names its operation defines.",
    )
    report_parser.add_argument(
        "--body-file",
        required=True,
        type=Path,
        help="File holding the report body: the prose below the frontmatter, "
        "addressed to the user.",
    )
    report_parser.add_argument(
        "--worker-name",
        default=None,
        help="This worker's own agent name (default: $MNGR_AGENT_NAME). Used "
        "only by the fallback delivery, which reads this worker's `lead_agent` "
        "label out of `mngr list`.",
    )

    destroy_parser = subparsers.add_parser(
        "destroy",
        help="Destroy a worker agent and, by default, every worker under it; "
        "branches survive unless --delete-branches.",
    )
    destroy_parser.add_argument("--name", required=True, help="Worker name to destroy.")
    destroy_parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Destroy only this agent; warn about each descendant left behind.",
    )
    destroy_parser.add_argument(
        "--delete-branches",
        action="store_true",
        help="Also delete the mngr/<name> branch of every destroyed agent "
        "(for a superseded or abandoned pass whose work is not wanted).",
    )

    stop_parser = subparsers.add_parser(
        "stop",
        help="Stop a worker agent and, by default, every worker under it "
        "(mngr stop --archive); worktrees and branches stay.",
    )
    stop_parser.add_argument("--name", required=True, help="Worker name to stop.")
    stop_parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Stop only this agent; warn about each descendant left running.",
    )

    return parser


def main(argv: Sequence[str] | None = None, runner: Runner | None = None) -> int:
    """CLI entry point. Tests inject ``runner`` to capture the launch argv lifecycle."""
    args = build_parser().parse_args(argv)

    if args.command == "launch":
        return _run_launch(args, runner)
    if args.command == "launch-sync":
        return _run_launch_sync(args, runner)
    if args.command == "report":
        return _run_report(args, runner)
    if args.command == "destroy":
        return _run_destroy(args, runner)
    if args.command == "stop":
        return _run_stop(args, runner)
    return _run_await(args)


if __name__ == "__main__":
    sys.exit(main())
