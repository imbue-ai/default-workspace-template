"""Tests for the update-staleness tracker and its app-shell surfacing."""

import subprocess
from pathlib import Path

from imbue.system_interface.server import _inject_update_staleness_meta_tag
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.update_staleness import STALENESS_TREE_MOVED
from imbue.system_interface.update_staleness import STALENESS_UPDATE_INTERRUPTED
from imbue.system_interface.update_staleness import UPDATE_APPLY_MARKER_REL
from imbue.system_interface.update_staleness import UPDATE_STALENESS_HEADER
from imbue.system_interface.update_staleness import UPDATE_STALENESS_META_TAG
from imbue.system_interface.update_staleness import UpdateStalenessTracker


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    _commit(repo, "initial")
    return repo


def _commit(repo: Path, message: str) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", message], cwd=repo, check=True
    )


# A path the running server holds in memory (vendored mngr is imported
# in-process), and paths the workspace repo moves for constantly without the
# server being any staler for it.
_RELEVANT_PATH = "system/vendor/mngr/libs/mngr/imbue/mngr/api/list.py"
_IRRELEVANT_PATHS = (
    "docs/VERSION_HISTORY.md",  # the apply's own ledger commit
    "data-notes.md",  # ordinary agent work
    "system/apps/system_interface/frontend/src/views/App.ts",  # rebuilt, not restarted
    ".agents/skills/update-self/SKILL.md",
)


def _commit_files(repo: Path, message: str, *relpaths: str) -> None:
    for relpath in relpaths:
        target = repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"changed for {message}\n")
        subprocess.run(["git", "add", relpath], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def _write_marker(repo: Path) -> None:
    marker = repo / UPDATE_APPLY_MARKER_REL
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"dri_agent": "lead", "phase": "merged"}')


def test_tracker_reports_nothing_while_the_tree_is_unmoved(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    assert tracker.staleness() is None


def test_tracker_reports_a_tree_that_moved_under_the_server(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    _commit_files(repo, "an update landed after this server started", _RELEVANT_PATH)
    assert tracker.staleness() == STALENESS_TREE_MOVED


def test_tracker_ignores_moves_that_leave_this_server_current(tmp_path: Path) -> None:
    # The workspace repo moves constantly for reasons the running server is
    # fully current for: minds commit their ordinary work here, the apply's
    # own version-history commit lands after the restart, and a frontend-only
    # apply rebuilds the served bundle without restarting. None of those may
    # show the banner -- a near-permanent false banner would erode the trust
    # the real one needs.
    repo = _make_repo(tmp_path)
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    _commit_files(repo, "ordinary work and bookkeeping", *_IRRELEVANT_PATHS)
    assert tracker.staleness() is None


def test_tracker_reads_a_landed_then_reverted_range_as_consistent(
    tmp_path: Path,
) -> None:
    # The comparison diffs trees, not commits: an apply that landed and was
    # then auto-reverted leaves HEAD moved but the content identical to what
    # this server started from.
    repo = _make_repo(tmp_path)
    _commit_files(repo, "pre-existing state", _RELEVANT_PATH)
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    _commit_files(repo, "the apply's merge", _RELEVANT_PATH)
    subprocess.run(
        ["git", "revert", "--no-edit", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert tracker.staleness() is None


def test_tracker_reports_an_interrupted_apply_over_a_moved_tree(tmp_path: Path) -> None:
    # The marker outranks the moved-tree comparison: while it exists the honest
    # description is "an update was interrupted", not merely "the tree moved".
    repo = _make_repo(tmp_path)
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    _commit_files(repo, "the interrupted apply's merge", _RELEVANT_PATH)
    _write_marker(repo)
    assert tracker.staleness() == STALENESS_UPDATE_INTERRUPTED


def test_tracker_degrades_to_not_stale_outside_a_repo(tmp_path: Path) -> None:
    tracker = UpdateStalenessTracker.capture(repo_root=tmp_path / "not-a-repo")
    assert tracker.startup_head is None
    assert tracker.staleness() is None


def test_app_shell_carries_the_staleness_header(tmp_path: Path) -> None:
    # The header rides on every app-shell response -- the built app and the
    # not-built placeholder alike -- so this needs no particular bundle state.
    repo = _make_repo(tmp_path)
    state = build_test_state()
    state.update_staleness = UpdateStalenessTracker.capture(repo_root=repo)
    _commit_files(repo, "moved after startup", _RELEVANT_PATH)

    client = create_application(state).test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers[UPDATE_STALENESS_HEADER] == STALENESS_TREE_MOVED


def test_app_shell_names_the_interrupted_variant_from_the_marker(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    state = build_test_state()
    state.update_staleness = UpdateStalenessTracker.capture(repo_root=repo)
    _write_marker(repo)

    client = create_application(state).test_client()
    response = client.get("/")

    assert response.headers[UPDATE_STALENESS_HEADER] == STALENESS_UPDATE_INTERRUPTED


def test_a_consistent_workspace_gets_no_header(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    state = build_test_state()
    state.update_staleness = UpdateStalenessTracker.capture(repo_root=repo)

    client = create_application(state).test_client()
    response = client.get("/")

    assert UPDATE_STALENESS_HEADER not in response.headers


def test_meta_tag_injection_names_the_variant_and_skips_when_consistent() -> None:
    shell = "<html><head></head><body>app</body></html>"
    injected = _inject_update_staleness_meta_tag(shell, STALENESS_TREE_MOVED)
    assert (
        f'<meta name="{UPDATE_STALENESS_META_TAG}" content="{STALENESS_TREE_MOVED}">'
        in injected
    )
    # A consistent workspace's shell carries no tag at all -- the frontend
    # banner keys off the tag's presence.
    assert _inject_update_staleness_meta_tag(shell, None) == shell
