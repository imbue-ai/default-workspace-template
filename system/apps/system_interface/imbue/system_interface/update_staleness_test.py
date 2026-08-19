"""Tests for the update-staleness tracker and its app-shell surfacing."""

import subprocess
from pathlib import Path
from unittest.mock import patch

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


def _write_marker(repo: Path) -> None:
    marker = repo / UPDATE_APPLY_MARKER_REL
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"dri_agent": "lead", "phase": "merged"}')


def test_tracker_reports_nothing_while_the_tree_is_unmoved(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    tracker = UpdateStalenessTracker(repo_root=repo)
    assert tracker.staleness() is None


def test_tracker_reports_a_tree_that_moved_under_the_server(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    tracker = UpdateStalenessTracker(repo_root=repo)
    _commit(repo, "an update landed after this server started")
    assert tracker.staleness() == STALENESS_TREE_MOVED


def test_tracker_reports_an_interrupted_apply_over_a_moved_tree(tmp_path: Path) -> None:
    # The marker outranks the HEAD comparison: while it exists the honest
    # description is "an update was interrupted", not merely "the tree moved".
    repo = _make_repo(tmp_path)
    tracker = UpdateStalenessTracker(repo_root=repo)
    _commit(repo, "the interrupted apply's merge")
    _write_marker(repo)
    assert tracker.staleness() == STALENESS_UPDATE_INTERRUPTED


def test_tracker_degrades_to_not_stale_outside_a_repo(tmp_path: Path) -> None:
    tracker = UpdateStalenessTracker(repo_root=tmp_path / "not-a-repo")
    assert tracker.staleness() is None


def test_app_shell_carries_the_staleness_header_and_meta_tag(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><head></head><body>app</body></html>")
    state = build_test_state()
    state.update_staleness = UpdateStalenessTracker(repo_root=repo)
    _commit(repo, "moved after startup")

    with patch("imbue.system_interface.server.STATIC_DIRECTORY", static_dir):
        client = create_application(state).test_client()
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers[UPDATE_STALENESS_HEADER] == STALENESS_TREE_MOVED
    assert (
        f'<meta name="{UPDATE_STALENESS_META_TAG}" content="{STALENESS_TREE_MOVED}">'
        in response.text
    )


def test_app_shell_names_the_interrupted_variant_from_the_marker(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><head></head><body>app</body></html>")
    state = build_test_state()
    state.update_staleness = UpdateStalenessTracker(repo_root=repo)
    _write_marker(repo)

    with patch("imbue.system_interface.server.STATIC_DIRECTORY", static_dir):
        client = create_application(state).test_client()
        response = client.get("/")

    assert response.headers[UPDATE_STALENESS_HEADER] == STALENESS_UPDATE_INTERRUPTED
    assert STALENESS_UPDATE_INTERRUPTED in response.text


def test_a_consistent_workspace_gets_no_header_and_no_tag(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><head></head><body>app</body></html>")
    state = build_test_state()
    state.update_staleness = UpdateStalenessTracker(repo_root=repo)

    with patch("imbue.system_interface.server.STATIC_DIRECTORY", static_dir):
        client = create_application(state).test_client()
        response = client.get("/")

    assert UPDATE_STALENESS_HEADER not in response.headers
    assert UPDATE_STALENESS_META_TAG not in response.text
