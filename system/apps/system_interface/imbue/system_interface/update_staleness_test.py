"""Tests for the update-staleness tracker and its app-shell surfacing."""

import ast
import importlib.util
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.server import _inject_update_staleness_meta_tag
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.update_staleness import STALENESS_TREE_MOVED
from imbue.system_interface.update_staleness import STALENESS_UPDATE_EMERGENCY
from imbue.system_interface.update_staleness import STALENESS_UPDATE_INTERRUPTED
from imbue.system_interface.update_staleness import UPDATE_APPLY_EMERGENCY_REL
from imbue.system_interface.update_staleness import UPDATE_APPLY_MARKER_REL
from imbue.system_interface.update_staleness import UPDATE_STALENESS_HEADER
from imbue.system_interface.update_staleness import UPDATE_STALENESS_META_TAG
from imbue.system_interface.update_staleness import UpdateStalenessTracker
from imbue.system_interface.update_staleness import WORKSPACE_ROOT_DIRECTORY
from imbue.system_interface.update_staleness import _is_path_relevant_to_this_server

# The shared `git_work_dir` fixture leaves an initialized repo with one commit
# but no committer identity in its config, so every later commit carries one.
_AS_TEST_AUTHOR = (
    "-c",
    "user.name=Test",
    "-c",
    "user.email=t@example.com",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *_AS_TEST_AUTHOR, *args], cwd=repo, check=True, capture_output=True)


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
        ("system/services/oom_priority/pyproject.toml", True),
        ("system/libs/tk_command_parsing/pyproject.toml", True),
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
        # The vendored mngr, imported in-process and shelled out to. Broader
        # than `.py` on purpose: this process reads that tree at runtime
        # through more than its Python.
        ("system/vendor/mngr/libs/mngr/imbue/mngr/api/list.py", True),
        ("system/vendor/mngr/libs/mngr/imbue/mngr/help/topics.toml", True),
        # ... except its documentation and its tests, which nothing holds in
        # memory -- and of which that tree has thousands.
        ("system/vendor/mngr/libs/mngr/README.md", False),
        ("system/vendor/mngr/libs/mngr/imbue/mngr/api/list_test.py", False),
        ("system/vendor/mngr/libs/mngr/imbue/mngr/test_end_to_end.py", False),
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


def _imported_workspace_package_source_paths() -> list[str]:
    """One representative source path per local workspace package this app depends on.

    Read from the app's own manifest rather than written out: the prefix lists
    on both sides of the restart knowledge are hand-maintained, and a
    dependency added without a prefix would leave a real skew showing no
    banner -- a silent failure the rest of the suite cannot see, because
    nothing else knows what this process imports.
    """
    repo_root = WORKSPACE_ROOT_DIRECTORY
    app_root = repo_root / "system/apps/system_interface"
    dependencies = set(tomllib.loads((app_root / "pyproject.toml").read_text())["project"]["dependencies"])
    # Requirement strings may carry a version specifier; the name is the head.
    names = {re.split(r"[<>=!~\[; ]", spec, maxsplit=1)[0] for spec in dependencies}
    local_packages = {
        tomllib.loads((manifest).read_text())["project"]["name"]: manifest.parent
        for parent in ("system/libs", "system/services", "system/apps", "system/vendor/mngr/libs")
        for manifest in (repo_root / parent).glob("*/pyproject.toml")
    }
    return sorted(
        f"{local_packages[name].relative_to(repo_root)}/src/module.py"
        for name in names & set(local_packages)
        if name != "system-interface"
    )


_IMPORTED_WORKSPACE_PACKAGE_SOURCE_PATHS = _imported_workspace_package_source_paths()


@pytest.mark.parametrize("path", _IMPORTED_WORKSPACE_PACKAGE_SOURCE_PATHS)
def test_every_imported_workspace_package_is_covered(path: str) -> None:
    assert _is_path_relevant_to_this_server(path)


def _commit_files(repo: Path, message: str, *relpaths: str) -> None:
    for relpath in relpaths:
        target = repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"changed for {message}\n")
        _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", message)


def _write_marker(repo: Path) -> None:
    marker = repo / UPDATE_APPLY_MARKER_REL
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"dri_agent": "lead", "phase": "merged"}')


def _write_emergency(repo: Path) -> None:
    record = repo / UPDATE_APPLY_EMERGENCY_REL
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text('{"reason": "rollback could not restore health"}')


def test_tracker_reports_nothing_while_the_tree_is_unmoved(git_work_dir: Path) -> None:
    repo = git_work_dir
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    assert tracker.staleness() is None


def test_tracker_reports_a_tree_that_moved_under_the_server(git_work_dir: Path) -> None:
    repo = git_work_dir
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    _commit_files(repo, "an update landed after this server started", _RELEVANT_PATH)
    assert tracker.staleness() == STALENESS_TREE_MOVED


def test_tracker_reports_a_moved_path_git_would_otherwise_quote(git_work_dir: Path) -> None:
    # Without ``-z`` git C-quotes a path with a non-ASCII byte, and the quoted
    # form starts with ``"`` -- which no prefix rule matches, so a real move
    # of such a file would show no banner.
    repo = git_work_dir
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    _commit_files(repo, "an update with a non-ASCII path", "system/vendor/mngr/libs/mngr/imbue/mngr/api/l\u00efst.py")
    assert tracker.staleness() == STALENESS_TREE_MOVED


def test_tracker_reuses_the_moved_tree_verdict_while_head_is_unchanged(git_work_dir: Path) -> None:
    # The shell route asks on every page load; the diff behind the verdict
    # runs once per HEAD. Proven by making the diff impossible after the first
    # ask -- the startup commit's object is removed, so a fresh diff fails and
    # would read as "no banner" -- while HEAD, the cache key, stays the same.
    repo = git_work_dir
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    assert tracker.startup_head is not None
    _commit_files(repo, "moved after startup", _RELEVANT_PATH)
    assert tracker.staleness() == STALENESS_TREE_MOVED

    (repo / ".git" / "objects" / tracker.startup_head[:2] / tracker.startup_head[2:]).unlink()
    fresh = UpdateStalenessTracker(repo_root=repo, startup_head=tracker.startup_head)
    assert fresh.staleness() is None
    assert tracker.staleness() == STALENESS_TREE_MOVED


def test_tracker_ignores_moves_that_leave_this_server_current(git_work_dir: Path) -> None:
    # The workspace repo moves constantly for reasons the running server is
    # fully current for: minds commit their ordinary work here, the apply's
    # own version-history commit lands after the restart, and a frontend-only
    # apply rebuilds the served bundle without restarting. None of those may
    # show the banner -- a near-permanent false banner would erode the trust
    # the real one needs.
    repo = git_work_dir
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    _commit_files(repo, "ordinary work and bookkeeping", *_IRRELEVANT_PATHS)
    assert tracker.staleness() is None


def test_tracker_reads_a_landed_then_reverted_range_as_consistent(
    git_work_dir: Path,
) -> None:
    # The comparison diffs trees, not commits: an apply that landed and was
    # then auto-reverted leaves HEAD moved but the content identical to what
    # this server started from.
    repo = git_work_dir
    _commit_files(repo, "pre-existing state", _RELEVANT_PATH)
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    _commit_files(repo, "the apply's merge", _RELEVANT_PATH)
    _git(repo, "revert", "--no-edit", "HEAD")
    assert tracker.staleness() is None


def test_tracker_reports_an_emergency_a_completed_rollback_leaves_invisible(
    git_work_dir: Path,
) -> None:
    # The state the other two checks cannot see, and the reason the record
    # exists: the apply clears its marker on the emergency exit, and its
    # rollback has already put the tree content back -- so a workspace whose
    # rollback failed to restore health looks, to both of them, consistent.
    repo = git_work_dir
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    _commit_files(repo, "the failed apply's merge", _RELEVANT_PATH)
    _git(repo, "revert", "--no-edit", "HEAD")
    assert tracker.staleness() is None
    _write_emergency(repo)
    assert tracker.staleness() == STALENESS_UPDATE_EMERGENCY


def test_tracker_reports_an_emergency_over_an_interrupted_apply(git_work_dir: Path) -> None:
    # An emergency does not resolve itself, so it must not be described as an
    # apply that is still part-way through and will finish or undo itself.
    repo = git_work_dir
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    _write_marker(repo)
    _write_emergency(repo)
    assert tracker.staleness() == STALENESS_UPDATE_EMERGENCY


def test_tracker_reports_an_interrupted_apply_over_a_moved_tree(git_work_dir: Path) -> None:
    # The marker outranks the moved-tree comparison: while it exists the honest
    # description is "an update was interrupted", not merely "the tree moved".
    repo = git_work_dir
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    _commit_files(repo, "the interrupted apply's merge", _RELEVANT_PATH)
    _write_marker(repo)
    assert tracker.staleness() == STALENESS_UPDATE_INTERRUPTED


def test_tracker_degrades_to_not_stale_outside_a_repo(tmp_path: Path, loguru_records: list[str]) -> None:
    tracker = UpdateStalenessTracker.capture(repo_root=tmp_path / "not-a-repo")
    assert tracker.startup_head is None
    assert tracker.staleness() is None
    # A detector that fails silently is worse than no detector: the whole point
    # is to make an invisible skew visible, so its own breakage must be
    # findable in the log rather than showing up as a banner that never comes.
    assert any("update-staleness" in record for record in loguru_records)


def test_a_git_failure_after_startup_is_logged_not_swallowed(git_work_dir: Path, loguru_records: list[str]) -> None:
    repo = git_work_dir
    tracker = UpdateStalenessTracker.capture(repo_root=repo)
    assert tracker.startup_head is not None
    loguru_records.clear()
    # The repo goes away under the running server -- standing in for the wedged
    # index or corrupt repo that would otherwise silence the banner forever.
    shutil.rmtree(repo / ".git")

    assert tracker.staleness() is None

    assert any("update-staleness" in record for record in loguru_records)


def _tracking_state(repo: Path) -> SystemInterfaceState:
    """A test state whose staleness tracker watches ``repo``.

    ``build_test_state`` captures against the developer's real checkout, which
    is never the tree a test moves.
    """
    state = build_test_state()
    state.update_staleness = UpdateStalenessTracker.capture(repo_root=repo)
    return state


def test_app_shell_carries_the_staleness_header(git_work_dir: Path) -> None:
    # The header rides on every app-shell response -- the built app and the
    # not-built placeholder alike -- so this needs no particular bundle state.
    repo = git_work_dir
    state = _tracking_state(repo)
    _commit_files(repo, "moved after startup", _RELEVANT_PATH)

    client = create_application(state).test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers[UPDATE_STALENESS_HEADER] == STALENESS_TREE_MOVED


def test_app_shell_names_the_interrupted_variant_from_the_marker(git_work_dir: Path) -> None:
    repo = git_work_dir
    state = _tracking_state(repo)
    _write_marker(repo)

    client = create_application(state).test_client()
    response = client.get("/")

    assert response.headers[UPDATE_STALENESS_HEADER] == STALENESS_UPDATE_INTERRUPTED


def test_the_placeholders_head_poll_does_not_ask_for_staleness(git_work_dir: Path) -> None:
    # The "frontend not built" placeholder polls this same route with HEAD
    # every ten seconds per open tab for the length of an outage, and that
    # response is deliberately built without reading anything. An outage is
    # exactly when the tree has moved, so asking here would fork git twice per
    # poll per tab. A real GET still carries the header.
    repo = git_work_dir
    state = _tracking_state(repo)
    _commit_files(repo, "moved after startup", _RELEVANT_PATH)
    client = create_application(state).test_client()

    assert UPDATE_STALENESS_HEADER not in client.head("/").headers
    assert client.get("/").headers[UPDATE_STALENESS_HEADER] == STALENESS_TREE_MOVED


def test_a_consistent_workspace_gets_no_header(git_work_dir: Path) -> None:
    repo = git_work_dir
    state = _tracking_state(repo)

    client = create_application(state).test_client()
    response = client.get("/")

    assert UPDATE_STALENESS_HEADER not in response.headers


def test_the_built_app_shell_carries_the_staleness_meta_tag(git_work_dir: Path, tmp_path: Path) -> None:
    """The meta tag is the only thing the banner reads, and only the *built*
    shell carries one -- every other server test here runs against the
    not-built placeholder, where nothing is injected at all.
    """
    repo = git_work_dir
    state = _tracking_state(repo)
    _commit_files(repo, "moved after startup", _RELEVANT_PATH)
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><head></head><body>app</body></html>")

    state.static_directory = static_dir
    response = create_application(state).test_client().get("/")
    # A workspace consistent with what it is serving gets no tag at all: the
    # tag's presence is the difference between banner and no banner.
    consistent_state = _tracking_state(repo)
    consistent_state.static_directory = static_dir
    consistent = create_application(consistent_state).test_client().get("/")

    assert response.status_code == 200
    assert f'<meta name="{UPDATE_STALENESS_META_TAG}" content="{STALENESS_TREE_MOVED}">' in response.text
    assert UPDATE_STALENESS_META_TAG not in consistent.text


def test_meta_tag_injection_names_the_variant_and_skips_when_consistent() -> None:
    shell = "<html><head></head><body>app</body></html>"
    injected = _inject_update_staleness_meta_tag(shell, STALENESS_TREE_MOVED)
    assert f'<meta name="{UPDATE_STALENESS_META_TAG}" content="{STALENESS_TREE_MOVED}">' in injected
    # A consistent workspace's shell carries no tag at all -- the frontend
    # banner keys off the tag's presence.
    assert _inject_update_staleness_meta_tag(shell, None) == shell


def test_meta_tag_injection_escapes_the_variant() -> None:
    # The variant is this module's own constant today; the tag is an attribute
    # value, so whatever lands there must not be able to close it.
    injected = _inject_update_staleness_meta_tag("<head></head>", 'x"><script>')
    assert 'content="x&quot;&gt;&lt;script&gt;"' in injected
    assert "<script>" not in injected


def _evaluate_path_expression(node: ast.expr, known: dict[str, str]) -> str | None:
    """Evaluate a string literal, a name, a ``Path(...)`` call, or a ``/`` join."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return known.get(node.id)
    if isinstance(node, ast.Call) and len(node.args) == 1:
        return _evaluate_path_expression(node.args[0], known)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _evaluate_path_expression(node.left, known)
        right = _evaluate_path_expression(node.right, known)
        return None if left is None or right is None else left + "/" + right
    return None


def _module_string_constants(source_path: Path) -> dict[str, str]:
    """Module-level path constants, resolved in declaration order.

    Read out of the source rather than imported: the three definitions of the
    apply's state paths sit in three isolation domains on purpose -- a
    stdlib-only skill script that must run when the tree around it is broken,
    the bootstrap package, and this app -- and none of them may import
    another. Reading is what is left, so a drift between them is at least
    caught here rather than at 3am in a workspace that will not come back.
    """
    constants: dict[str, str] = {}
    for node in ast.parse(source_path.read_text()).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = _evaluate_path_expression(node.value, constants)
        if value is not None:
            constants[target.id] = value
    return constants


def test_the_three_definitions_of_the_apply_state_paths_agree() -> None:
    """This app, bootstrap and the apply script must name the same files.

    Each has its own copy because none can import another, so nothing but this
    stops them drifting -- and a drift is silent in the worst way: the banner
    would simply never fire, and the boot-time recovery would never find an
    interrupted apply to roll back.
    """
    repo_root = WORKSPACE_ROOT_DIRECTORY
    script = _module_string_constants(repo_root / ".agents/skills/update-self/scripts/update_self.py")
    bootstrap = _module_string_constants(repo_root / "system/libs/bootstrap/src/bootstrap/manager.py")

    state_dir = script["STATE_DIR_REL"]
    assert state_dir + "/" + script["MARKER_FILENAME"] == UPDATE_APPLY_MARKER_REL
    assert state_dir + "/" + script["EMERGENCY_FILENAME"] == UPDATE_APPLY_EMERGENCY_REL
    # bootstrap composes its marker path from its own STATE_DIR.
    assert bootstrap["UPDATE_APPLY_MARKER"] == UPDATE_APPLY_MARKER_REL


# The banner's message table, the other half of every variant this module
# emits. Read as text because Python cannot import TypeScript.
_BANNER_SOURCE_REL = "system/apps/system_interface/frontend/src/views/UpdateStalenessBanner.ts"


def test_the_banner_has_a_message_for_every_variant_and_no_others() -> None:
    """The variant strings are written twice, once per language.

    Nothing else notices a rename: the backend would go on stamping the header
    and the meta tag while the banner silently stopped rendering -- exactly the
    silent skew this detector exists to make visible, and invisible to both
    suites because each side is individually self-consistent.
    """
    source = (WORKSPACE_ROOT_DIRECTORY / _BANNER_SOURCE_REL).read_text()
    table = source.partition("new Map<string, string>([")[2].partition("]);")[0]
    assert set(re.findall(r'\[\s*"([^"]+)",', table)) == {
        STALENESS_UPDATE_EMERGENCY,
        STALENESS_UPDATE_INTERRUPTED,
        STALENESS_TREE_MOVED,
    }


def _load_apply_script() -> ModuleType:
    """Import the stdlib-only apply script by path.

    The app cannot depend on it (it is a skill script, staged and run on trees
    where this app may not even be installed), but a test can read it where it
    lies -- which is the only way to hold the two halves of the restart
    knowledge against each other.
    """
    name = "update_self_under_test"
    if name in sys.modules:
        return sys.modules[name]
    script = WORKSPACE_ROOT_DIRECTORY / ".agents/skills/update-self/scripts/update_self.py"
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the script's dataclasses resolve their own
    # annotations through sys.modules, and fail on a module that is not there.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Paths the apply restarts the services agent for, and whether THIS process is
# one of the things left stale by them. The two rules are written separately
# because neither side can import the other, and the split is deliberate: the
# apply asks "does anything live need a restart", this app asks "am I running
# old code". The False rows are the whole reason the second question exists.
_RESTART_REQUIRING_PATHS = (
    # The vendored mngr: this process imports it in-process and shells out to
    # it, so a tree advance leaves it running old code against new state.
    ("system/vendor/mngr/libs/mngr/imbue/mngr/api/list.py", True),
    # The settings file this process re-reads on every request.
    (".mngr/settings.toml", True),
    # Restart-requiring for other processes, not this one: nothing here is
    # imported by the system interface.
    ("system/supervisord.conf", False),
    ("system/libs/bootstrap/src/bootstrap/manager.py", False),
) + tuple(
    # Every workspace package this process imports: the apply must restart the
    # services agent for it, and this server is stale after it.
    (path, True)
    for path in _IMPORTED_WORKSPACE_PACKAGE_SOURCE_PATHS
)


@pytest.mark.parametrize(("path", "leaves_this_server_stale"), _RESTART_REQUIRING_PATHS)
def test_the_restart_rule_and_the_staleness_rule_stay_in_step(path: str, leaves_this_server_stale: bool) -> None:
    """Every path the apply restarts for is classified here too, one way or the other.

    Without this, adding a restart-requiring prefix to ``classify_path`` and
    forgetting this module leaves a real skew showing no banner -- and nothing
    else in either suite can see that, because each side is individually
    self-consistent.
    """
    classify_path = _load_apply_script().classify_path
    assert classify_path(path).requires_restart is True
    assert _is_path_relevant_to_this_server(path) is leaves_this_server_stale
