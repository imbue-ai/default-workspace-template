"""`record_publish.py` writing the ledger of a real workspace-shaped repo."""

import re
import subprocess
import sys
from pathlib import Path

import pytest
import record_publish
from publish_testing import git, init_repo

_SCRIPT = Path(__file__).resolve().parent / "record_publish.py"
_LEDGER = "docs/VERSION_HISTORY.md"
_TODAY = "2026-10-20"


def _shipped_ledger() -> str:
    """The ledger a template release ships, before any workspace has written to it.

    Not this checkout's own `docs/VERSION_HISTORY.md`: in a workspace that one
    already records where the workspace came from.
    """
    return record_publish._update_self_module("update_ledger").VERSION_HISTORY_STARTER


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A template release tagged minds-v0.9.1, then bootstrap's marker, then work."""
    root = init_repo(tmp_path / "workspace")
    (root / "docs").mkdir()
    (root / _LEDGER).write_text(_shipped_ledger())
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Template release")
    git(root, "tag", "minds-v0.9.1")
    git(root, "commit", "-q", "--allow-empty", "-m", "Initial workspace commit")
    (root / "app.py").write_text("x = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Build the app")
    return root


def _record(
    workspace: Path,
    note: str,
    *extra: str,
    slug: str = "inbox",
    sha: str = "HEAD",
    repo: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--workspace",
            str(workspace),
            "--slug",
            slug,
            "--repo-url",
            f"github.com/octo/{repo or slug}",
            "--note",
            note,
            "--source-sha",
            sha,
            "--date",
            _TODAY,
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )


def _templates_block(workspace: Path, slug: str, repo: str | None = None) -> list[str]:
    lines = (workspace / _LEDGER).read_text().splitlines()
    start = lines.index(f"### {slug}  --  github.com/octo/{repo or slug}")
    block = []
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        if line.strip():
            block.append(line)
    return block


def test_a_first_publish_records_v1_and_seeds_where_the_workspace_started(
    workspace: Path,
) -> None:
    head = git(workspace, "rev-parse", "--short=7", "HEAD")
    marker = git(workspace, "rev-parse", "--short=7", "HEAD~1")
    (workspace / "app.py").write_text("x = 2\n")

    completed = _record(workspace, "first published")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "v1"
    assert _templates_block(workspace, "inbox") == [
        f"- v1  {_TODAY}  {'first published':<35}{head}"
    ]
    ledger = (workspace / _LEDGER).read_text()
    assert re.search(
        rf"^- \d{{4}}-\d\d-\d\d  created from minds-v0\.9\.1 +{marker}$", ledger, re.M
    )
    assert (
        git(workspace, "log", "-1", "--format=%s")
        == "version history: published template inbox v1"
    )
    assert git(workspace, "show", "--name-only", "--format=", "HEAD") == _LEDGER
    assert git(workspace, "status", "--porcelain") == "M app.py"


def test_the_entry_is_committed_alone_and_other_staged_work_stays_staged(
    workspace: Path,
) -> None:
    (workspace / "app.py").write_text("x = 2\n")
    git(workspace, "add", "app.py")

    assert _record(workspace, "first published").returncode == 0

    assert git(workspace, "show", "--name-only", "--format=", "HEAD") == _LEDGER
    assert git(workspace, "diff", "--cached", "--name-only") == "app.py"


def test_recording_the_same_publish_again_changes_nothing(workspace: Path) -> None:
    source = git(workspace, "rev-parse", "HEAD")
    assert _record(workspace, "first published", sha=source).returncode == 0
    commits = git(workspace, "rev-list", "--count", "HEAD")

    again = _record(workspace, "first published", sha=source)

    assert again.returncode == 0
    assert "already recorded" in again.stdout
    assert git(workspace, "rev-list", "--count", "HEAD") == commits


def test_an_update_appends_the_next_version_under_the_same_heading(
    workspace: Path,
) -> None:
    assert _record(workspace, "first published").returncode == 0
    (workspace / "app.py").write_text("x = 3\n")
    git(workspace, "commit", "-qam", "Add reminders")
    head = git(workspace, "rev-parse", "--short=7", "HEAD")

    completed = _record(workspace, "adds a reminders column")

    assert completed.stdout.strip() == "v2"
    assert _templates_block(workspace, "inbox")[-1] == (
        f"- v2  {_TODAY}  {'adds a reminders column':<35}{head}"
    )
    assert git(workspace, "log", "-1", "--format=%s") == (
        "version history: updated template inbox to v2"
    )


def test_a_shorter_note_at_the_same_sha_is_a_new_version_not_a_retry(
    workspace: Path,
) -> None:
    """Only the exact note counts as already recorded, not one it is a part of."""
    source = git(workspace, "rev-parse", "HEAD")
    assert _record(workspace, "adds a reminders column", sha=source).returncode == 0

    completed = _record(workspace, "adds a reminders", sha=source)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "v2"
    assert git(workspace, "log", "-1", "--format=%s") == (
        "version history: updated template inbox to v2"
    )


def test_an_update_of_a_template_the_ledger_never_recorded_takes_its_published_version(
    workspace: Path,
) -> None:
    """Published before the ledger recorded it: there is no entry to count from."""
    head = git(workspace, "rev-parse", "--short=7", "HEAD")

    completed = _record(workspace, "adds a reminders column", "--version", "3")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "v3"
    assert _templates_block(workspace, "inbox") == [
        f"- v3  {_TODAY}  {'adds a reminders column':<35}{head}"
    ]
    assert git(workspace, "log", "-1", "--format=%s") == (
        "version history: updated template inbox to v3"
    )


def test_a_version_the_ledger_contradicts_is_refused_and_nothing_is_written(
    workspace: Path,
) -> None:
    assert _record(workspace, "first published").returncode == 0
    ledger = (workspace / _LEDGER).read_text()

    completed = _record(workspace, "adds a reminders column", "--version", "5")

    assert completed.returncode == 1
    assert "this publish is v2, not v5" in completed.stderr
    assert (workspace / _LEDGER).read_text() == ledger


def test_two_templates_from_one_commit_are_recorded_separately(
    workspace: Path,
) -> None:
    assert _record(workspace, "first published", slug="inbox").returncode == 0

    completed = _record(workspace, "first published", slug="digest")

    assert completed.stdout.strip() == "v1"
    assert len(_templates_block(workspace, "inbox")) == 1
    assert len(_templates_block(workspace, "digest")) == 1


def test_a_slug_published_afresh_to_another_repo_starts_its_own_history(
    workspace: Path,
) -> None:
    """A repo that had to be deleted is no predecessor: its replacement is a v1."""
    assert _record(workspace, "first published").returncode == 0

    completed = _record(workspace, "first published", repo="inbox-fresh")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "v1"
    assert len(_templates_block(workspace, "inbox")) == 1
    assert _templates_block(workspace, "inbox", repo="inbox-fresh")[0].startswith(
        "- v1  "
    )
    assert git(workspace, "log", "-1", "--format=%s") == (
        "version history: published template inbox v1"
    )


def test_a_long_note_still_leaves_two_spaces_before_the_sha(workspace: Path) -> None:
    note = "rewrites the digest to group by sender and adds a weekly summary"
    head = git(workspace, "rev-parse", "--short=7", "HEAD")

    assert _record(workspace, note).returncode == 0

    assert _templates_block(workspace, "inbox") == [f"- v1  {_TODAY}  {note}  {head}"]


def test_a_deleted_ledger_is_recreated_from_the_shipped_starter(
    workspace: Path,
) -> None:
    git(workspace, "rm", "-q", _LEDGER)
    git(workspace, "commit", "-qm", "drop the ledger")

    completed = _record(workspace, "first published")

    assert completed.returncode == 0, completed.stderr
    ledger = (workspace / _LEDGER).read_text()
    assert ledger.startswith("# Version history\n")
    assert (
        ledger.index("## Templates")
        < ledger.index("### inbox")
        < ledger.index("## Adopted templates")
    )


def test_a_locked_index_says_the_template_is_published_and_to_commit_by_hand(
    workspace: Path,
) -> None:
    """Another git process holding the index fails the add, after the push already happened."""
    (workspace / ".git/index.lock").write_text("")

    completed = _record(workspace, "first published")

    assert completed.returncode == 1
    assert "IS published" in completed.stderr
    assert "commit that one file by hand" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert _templates_block(workspace, "inbox")[0].startswith("- v1  ")


def test_a_rerun_after_a_failed_commit_commits_the_entry_it_left_behind(
    workspace: Path,
) -> None:
    """The entry is already in the file, so only the commit is left to do."""
    lock = workspace / ".git/index.lock"
    lock.write_text("")
    assert _record(workspace, "first published").returncode == 1
    lock.unlink()

    completed = _record(workspace, "first published")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "v1"
    assert git(workspace, "show", "--name-only", "--format=", "HEAD") == _LEDGER
    assert git(workspace, "status", "--porcelain") == ""
    assert len(_templates_block(workspace, "inbox")) == 1


def test_a_source_sha_that_does_not_resolve_says_why_and_writes_nothing(
    workspace: Path,
) -> None:
    completed = _record(workspace, "first published", sha="no-such-commit")

    assert completed.returncode == 1
    assert "--source-sha 'no-such-commit' is not a commit" in completed.stderr
    assert git(workspace, "status", "--porcelain") == ""
