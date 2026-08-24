"""Tests for the update-staleness tracker and its app-shell surfacing."""

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from imbue.system_interface.server import _inject_update_staleness_meta_tag
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.update_staleness import STALENESS_TREE_MOVED
from imbue.system_interface.update_staleness import STALENESS_UPDATE_INTERRUPTED
from imbue.system_interface.update_staleness import UPDATE_APPLY_MARKER_REL
from imbue.system_interface.update_staleness import UPDATE_STALENESS_HEADER
from imbue.system_interface.update_staleness import UPDATE_STALENESS_META_TAG
from imbue.system_interface.update_staleness import UpdateStalenessTracker
from imbue.system_interface.update_staleness import _is_path_relevant_to_this_server


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
# server being any staler for it: the apply's own ledger commit, ordinary
# agent work, frontend source (whose bundle is rebuilt without a restart),
# and skill prose.
_RELEVANT_PATH = "system/vendor/mngr/libs/mngr/imbue/mngr/api/list.py"
_IRRELEVANT_PATHS = (
    "docs/VERSION_HISTORY.md",
    "data-notes.md",
    "system/apps/system_interface/frontend/src/views/App.ts",
    ".agents/skills/update-self/SKILL.md",
)


@pytest.mark.parametrize(
    ("path", "is_relevant"),
    [
        # The settings file the long-lived mngr config parser re-reads.
        (".mngr/settings.toml", True),
        # Every manifest the served environment was resolved from.
        ("pyproject.toml", True),
        ("uv.lock", True),
        ("system/apps/system_interface/pyproject.toml", True),
        # The backend this process is running.
        ("system/apps/system_interface/imbue/system_interface/server.py", True),
        # ... but not its tests, which no running process holds.
        ("system/apps/system_interface/imbue/system_interface/server_test.py", False),
        ("system/apps/system_interface/imbue/system_interface/test_layout_pipeline.py", False),
        # Workspace libraries imported in-process through editable installs.
        ("system/services/oom_priority/src/oom_priority/bands.py", True),
        ("system/libs/tk_command_parsing/src/tk_command_parsing/parser.py", True),
        # A workspace library this process does not import.
        ("system/libs/bootstrap/src/bootstrap/manager.py", False),
        # The vendored mngr, imported in-process and shelled out to ...
        ("system/vendor/mngr/libs/mngr/imbue/mngr/api/list.py", True),
        # ... except its documentation, which nothing holds in memory.
        ("system/vendor/mngr/libs/mngr/README.md", False),
        # The frontend: its bundle is rebuilt on disk without a restart.
        ("system/apps/system_interface/frontend/src/views/App.ts", False),
        # Things minds commit constantly and are never staler for.
        ("docs/VERSION_HISTORY.md", False),
        (".agents/skills/update-self/SKILL.md", False),
        ("data-notes.md", False),
    ],
)
def test_relevance_rules(path: str, is_relevant: bool) -> None:
    assert _is_path_relevant_to_this_server(path) is is_relevant


def test_every_imported_workspace_package_is_covered() -> None:
    """Every workspace package the app depends on must make it stale.

    The prefix list is hand-maintained, and a dependency added without a prefix
    would leave a real skew showing no banner -- a silent failure the rest of
    the suite cannot see, because nothing else knows what this process imports.
    """
    app_root = Path(__file__).resolve().parents[2]
    repo_root = app_root.parents[2]
    dependencies = set(
        tomllib.loads((app_root / "pyproject.toml").read_text())["project"]["dependencies"]
    )
    # Requirement strings may carry a version specifier; the name is the head.
    names = {re.split(r"[<>=!~\[; ]", spec, maxsplit=1)[0] for spec in dependencies}
    local_packages = {
        tomllib.loads((manifest).read_text())["project"]["name"]: manifest.parent
        for parent in ("system/libs", "system/services", "system/apps", "system/vendor/mngr/libs")
        for manifest in (repo_root / parent).glob("*/pyproject.toml")
    }

    uncovered = [
        name
        for name in sorted(names & set(local_packages))
        if name != "system-interface"
        and not _is_path_relevant_to_this_server(
            f"{local_packages[name].relative_to(repo_root)}/src/module.py"
        )
    ]

    assert uncovered == []


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


def test_tracker_degrades_to_not_stale_outside_a_repo(
    tmp_path: Path, loguru_records: list[str]
) -> None:
    tracker = UpdateStalenessTracker.capture(repo_root=tmp_path / "not-a-repo")
    assert tracker.startup_head is None
    assert tracker.staleness() is None
    # A detector that fails silently is worse than no detector: the whole point
    # is to make an invisible skew visible, so its own breakage must be
    # findable in the log rather than showing up as a banner that never comes.
    assert any("update-staleness" in record for record in loguru_records)


def test_a_git_failure_after_startup_is_logged_not_swallowed(
    tmp_path: Path, loguru_records: list[str]
) -> None:
    repo = _make_repo(tmp_path)
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    assert tracker.startup_head is not None
    loguru_records.clear()
    # The repo goes away under the running server -- standing in for the wedged
    # index or corrupt repo that would otherwise silence the banner forever.
    shutil.rmtree(repo / ".git")

    assert tracker.staleness() is None

    assert any("update-staleness" in record for record in loguru_records)


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


def test_the_placeholders_head_poll_does_not_ask_for_staleness(tmp_path: Path) -> None:
    # The "frontend not built" placeholder polls this same route with HEAD
    # every ten seconds per open tab for the length of an outage, and that
    # response is deliberately built without reading anything. An outage is
    # exactly when the tree has moved, so asking here would fork git twice per
    # poll per tab. A real GET still carries the header.
    repo = _make_repo(tmp_path)
    state = build_test_state()
    state.update_staleness = UpdateStalenessTracker.capture(repo_root=repo)
    _commit_files(repo, "moved after startup", _RELEVANT_PATH)
    client = create_application(state).test_client()

    assert UPDATE_STALENESS_HEADER not in client.head("/").headers
    assert client.get("/").headers[UPDATE_STALENESS_HEADER] == STALENESS_TREE_MOVED


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
