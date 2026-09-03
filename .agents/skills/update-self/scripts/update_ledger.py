"""The ``docs/VERSION_HISTORY.md`` ledger entry a landed update records."""

from __future__ import annotations

import subprocess
from pathlib import Path

from update_runtime import ApplyError
from update_runtime import Runner
from update_runtime import git_out

_VERSION_HISTORY_REL = "docs/VERSION_HISTORY.md"

_WORKSPACE_HEADING = "## Workspace"

# Notes are padded to this width so the trailing shas line up; a note this wide
# or wider takes a plain two-space gap instead (``created from minds-v0.3.NN``
# is exactly 26 characters, so a bare pad would land the sha flush against it).
_LEDGER_NOTE_WIDTH = 26

# The canonical starter, recreated when the file was deleted since creation.
# Byte-identical to the ``docs/VERSION_HISTORY.md`` the template ships;
# ``publish-template``, ``update-published-template`` and
# ``update-installed-template`` all recreate it by reference to here.
_VERSION_HISTORY_STARTER = """\
# Version history

Where this workspace came from, what it has migrated in, what it has published,
and the templates it has adopted. Entries are appended automatically -- by
`update-self` when it lands a template update, by `migrate-workspace` when it
pulls another workspace in, by `publish-template` and
`update-published-template` when they publish, and by
`update-installed-template` when it pulls a newer version of an adopted
template -- and earlier lines are never rewritten. Each Workspace, Migrations,
and Templates line ends in the commit it was cut from.

## Workspace

## Migrations

## Templates

## Adopted templates

Each template this mind has adopted and the version it is on;
`update-installed-template` appends here when it pulls a newer version.
"""


def _ledger_line(date: str, note: str, sha: str) -> str:
    padded = note.ljust(_LEDGER_NOTE_WIDTH)
    if len(padded) - len(note) < 2:
        padded = note + "  "
    return f"- {date}  {padded}{sha}"


def _insert_under_workspace(lines: list[str], new_line: str, *, first: bool) -> None:
    """Insert ``new_line`` under ``## Workspace`` -- as the section's first line
    (the origin seed: the oldest event) or after its last existing line (an
    update entry). Existing lines are never re-flowed."""
    heading = lines.index(_WORKSPACE_HEADING)
    end = heading + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    entries = [index for index in range(heading + 1, end) if lines[index].strip() != ""]
    if first or not entries:
        position = entries[0] if entries else heading + 2
        # An empty section is ``## Workspace`` + a blank line; landing past the
        # section's end means the blank line was missing -- insert directly
        # after the heading instead.
        position = min(position, end)
    else:
        position = entries[-1] + 1
    lines.insert(position, new_line)
    # Keep a blank line between the entries and the next heading.
    after = position + 1
    if after < len(lines) and lines[after].startswith("## "):
        lines.insert(after, "")


def _origin_line(repo_root: Path, runner: Runner) -> str:
    """The one-time ``created from`` seed for ``## Workspace``.

    The template base is the OLDEST first-parent template-state marker (an
    ``update-self:`` merge or the ``Initial workspace commit``), falling back to
    the first-parent root; its date, version and sha come from that commit
    itself, so seeding late still records when the workspace was created. The
    version uses ``git describe`` (reachability), never ``--points-at``: no tag
    is ever *on* a template base, only on an ancestor of it.
    """
    log = git_out(
        runner, repo_root, ["log", "--first-parent", "--format=%H %s", "HEAD"]
    )
    creation = ""
    for line in log.splitlines():
        sha, _, subject = line.partition(" ")
        if subject.startswith("update-self:") or subject == "Initial workspace commit":
            creation = sha  # keep walking: the log is newest-first, we want the oldest
    if not creation:
        revs = git_out(runner, repo_root, ["rev-list", "--first-parent", "HEAD"])
        creation = revs.splitlines()[-1] if revs else "HEAD"
    date = git_out(
        runner, repo_root, ["log", "-1", "--format=%ad", "--date=short", creation]
    )
    short = git_out(runner, repo_root, ["rev-parse", "--short=7", creation])
    describe = runner.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "minds-v*", creation],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    version = (getattr(describe, "stdout", "") or "").strip()
    note = (
        f"created from {version}" if version else "created from the workspace template"
    )
    return _ledger_line(date, note, short)


def write_version_history_entry(
    repo_root: Path,
    runner: Runner,
    target_ref: str,
    merge_sha: str,
    today: str,
) -> None:
    """Record ``updated to <target_ref>`` in ``docs/VERSION_HISTORY.md``, committed.

    Landing an update is what makes the workspace a new version, so the entry
    belongs in the git tree as part of the same apply -- never left to a later
    turn. Append-only and idempotent: a ``## Workspace`` line already carrying
    this exact note and this exact 7-char sha means the update is recorded and
    nothing is written, so a resumed apply never duplicates it. The commit
    stages exactly this one file and must never carry an ``update-self:``
    subject (that prefix is the template-state marker the merge commit alone
    owns). It skips hooks like the rollback commit does -- this runs after the
    marker is cleared, so a hook that refuses it would leave the file staged
    and every later apply/recover refusing the dirty tree. Should the commit
    still fail, the file is unstaged and :class:`LedgerCommitError` names it.
    """
    path = repo_root / _VERSION_HISTORY_REL
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_VERSION_HISTORY_STARTER)
    lines = path.read_text().splitlines()
    if _WORKSPACE_HEADING not in lines:
        # A hand-mangled file: append the section rather than losing the entry.
        lines.extend(["", _WORKSPACE_HEADING, ""])
    if not any("created from" in line for line in lines):
        _insert_under_workspace(lines, _origin_line(repo_root, runner), first=True)
    short = git_out(runner, repo_root, ["rev-parse", "--short=7", merge_sha])
    note = f"updated to {target_ref}"
    if any(note in line and short in line for line in lines):
        return  # already recorded; a retried landing must be a no-op
    _insert_under_workspace(lines, _ledger_line(today, note, short), first=False)
    path.write_text("\n".join(lines) + "\n")
    runner.run(
        ["git", "add", _VERSION_HISTORY_REL],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        runner.run(
            [
                "git",
                "commit",
                "--no-verify",
                "-m",
                f"version history: updated to {target_ref}",
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        runner.run(
            ["git", "reset", "-q", "--", _VERSION_HISTORY_REL],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        raise LedgerCommitError(
            f"committing {_VERSION_HISTORY_REL} failed ({exc}); the entry is left "
            f"in {_VERSION_HISTORY_REL} as an unstaged working-tree change -- commit "
            "it by hand (it is the only change), or discard it, before the next "
            "apply, which refuses a dirty tree"
        ) from exc


class LedgerCommitError(ApplyError):
    """The version-history entry was written but could not be committed.

    Raised after the file has been unstaged again, so the message can tell the
    caller exactly what is left behind and what to do about it.
    """
