import ast
import re
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

import pytest
from packaging.version import Version

# scripts/release.py uses bare imports of its sibling modules (e.g.
# `from changelog_release_utils import ...`), matching how it's invoked
# (`uv run scripts/release.py ...`). Make those resolvable for pytest by
# adding scripts/ to sys.path before importing release.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from scripts.release import DEPENDENCY_COOLDOWN  # noqa: E402
from scripts.release import _gate_release_on_pending_changelog_entries  # noqa: E402
from scripts.release import _pluralize_entry  # noqa: E402
from scripts.release import _realign_dep_string  # noqa: E402
from scripts.release import temp_ref_of_working_tree  # noqa: E402
from scripts.release import update_exclude_newer  # noqa: E402
from scripts.utils import REPO_ROOT  # noqa: E402
from scripts.utils import iter_standalone_project_dirs  # noqa: E402


def _write_changelog_entry(tmp_path: Path, name: str, content: str = "- entry", project: str = "mngr") -> None:
    """Drop an entry under the per-project in-project layout (libs/<project>/changelog/<name>).

    Also stamps a stub ``pyproject.toml`` so ``all_known_projects()`` discovers the project.
    """
    project_dir = tmp_path / "libs" / project
    (project_dir / "changelog").mkdir(parents=True, exist_ok=True)
    (project_dir / "pyproject.toml").write_text("")
    (project_dir / "changelog" / name).write_text(content)


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "entries"),
        (1, "entry"),
        (5, "entries"),
    ],
)
def test_pluralize_entry(count: int, expected: str) -> None:
    assert _pluralize_entry(count) == expected


@pytest.mark.parametrize("dry_run", [False, True])
def test_gate_returns_true_when_no_pending_entries(
    tmp_path: Path, dry_run: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _gate_release_on_pending_changelog_entries(tmp_path, dry_run=dry_run)
    assert result is True
    assert capsys.readouterr().out == ""


def test_gate_warns_and_returns_true_in_dry_run_with_pending_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_changelog_entry(tmp_path, "fake-entry.md")
    result = _gate_release_on_pending_changelog_entries(tmp_path, dry_run=True)
    assert result is True
    output = capsys.readouterr().out
    assert "WARNING" in output
    assert "1 pending changelog entry" in output
    assert "libs/mngr/changelog/fake-entry.md" in output


def test_gate_blocks_and_returns_false_with_pending_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_changelog_entry(tmp_path, "fake-a.md", project="mngr")
    _write_changelog_entry(tmp_path, "fake-b.md", project="mngr_lima")
    result = _gate_release_on_pending_changelog_entries(tmp_path, dry_run=False)
    assert result is False
    captured = capsys.readouterr()
    # The blocking-error path writes to stderr (matches the rest of
    # release.py's 'ERROR:' convention); stdout should stay empty.
    assert captured.out == ""
    err = captured.err
    assert "ERROR" in err
    assert "2 pending changelog entries" in err
    assert "libs/mngr/changelog/fake-a.md" in err
    assert "libs/mngr_lima/changelog/fake-b.md" in err
    # The error path points the user at the on-demand trigger recipe.
    assert "just changelog-trigger" in err


def test_realign_dep_string_realigns_existing_pin_regardless_of_force() -> None:
    # An existing == pin is always realigned, whether or not the dep is forced.
    assert _realign_dep_string("imbue-mngr==0.2.8", "0.2.10", force_pin=False) == "imbue-mngr==0.2.10"
    assert _realign_dep_string("imbue-mngr==0.2.8", "0.2.10", force_pin=True) == "imbue-mngr==0.2.10"


def test_realign_dep_string_leaves_unpinned_alone_without_force() -> None:
    # A deliberately-unpinned internal dep (non-publishable consumer) stays unpinned.
    assert _realign_dep_string("imbue-mngr", "0.2.10", force_pin=False) == "imbue-mngr"
    assert _realign_dep_string("imbue-mngr>=0.2.0", "0.2.10", force_pin=False) == "imbue-mngr>=0.2.0"


def test_realign_dep_string_introduces_pin_when_forced() -> None:
    # A publishable wheel must pin its internal deps, so force_pin adds the pin
    # (collapsing any looser specifier).
    assert _realign_dep_string("imbue-mngr", "0.2.10", force_pin=True) == "imbue-mngr==0.2.10"
    assert _realign_dep_string("imbue-mngr>=0.2.0", "0.2.10", force_pin=True) == "imbue-mngr==0.2.10"


def test_realign_dep_string_no_op_when_already_correct() -> None:
    assert _realign_dep_string("imbue-mngr==0.2.10", "0.2.10", force_pin=True) == "imbue-mngr==0.2.10"


def test_realign_dep_string_rejects_extras_and_markers() -> None:
    # The collapse-to-`name==version` form would silently drop an extra or marker;
    # internal deps never carry one, so guard loudly if that assumption breaks.
    with pytest.raises(AssertionError):
        _realign_dep_string("imbue-mngr==0.2.8 ; python_version < '3.12'", "0.2.10", force_pin=False)
    with pytest.raises(AssertionError):
        _realign_dep_string("imbue-mngr[extra]==0.2.8", "0.2.10", force_pin=False)


def _write_root_pyproject(tmp_path: Path, exclude_newer: str) -> Path:
    """Write a minimal root pyproject.toml carrying a `[tool.uv] exclude-newer`.

    Includes an unrelated key under [tool.uv] so the tests can assert that
    update_exclude_newer rewrites only the cutoff and preserves the rest.
    """
    path = tmp_path / "pyproject.toml"
    path.write_text(
        f'[tool.uv]\nexclude-newer = "{exclude_newer}"\n\n[tool.uv.sources]\nimbue-common = {{ workspace = true }}\n'
    )
    return path


def test_update_exclude_newer_advances_stale_cutoff(tmp_path: Path) -> None:
    # A cutoff well older than two weeks before the release date is advanced to
    # exactly (release_date - 2 weeks), and unrelated config is preserved.
    path = _write_root_pyproject(tmp_path, "2026-01-01T00:00:00Z")
    result = update_exclude_newer(path, date(2026, 5, 27))
    assert result == "2026-05-13T00:00:00Z"
    doc = tomllib.loads(path.read_text())
    assert doc["tool"]["uv"]["exclude-newer"] == "2026-05-13T00:00:00Z"
    assert doc["tool"]["uv"]["sources"]["imbue-common"] == {"workspace": True}


def test_update_exclude_newer_keeps_recent_cutoff(tmp_path: Path) -> None:
    # A cutoff younger than the cooldown window (only 4 days before the release
    # date) must be left untouched: advancing it would push it back and re-exclude
    # whatever freshly-pinned dep it was set to admit.
    path = _write_root_pyproject(tmp_path, "2026-05-23T00:00:00Z")
    original = path.read_text()
    result = update_exclude_newer(path, date(2026, 5, 27))
    assert result is None
    assert path.read_text() == original


def test_update_exclude_newer_noop_at_window_boundary(tmp_path: Path) -> None:
    # A cutoff exactly at (release_date - 2 weeks) is a no-op: max() ties to the
    # current value, so no rewrite happens.
    path = _write_root_pyproject(tmp_path, "2026-05-13T00:00:00Z")
    result = update_exclude_newer(path, date(2026, 5, 27))
    assert result is None


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_temp_ref_of_working_tree_snapshots_unstaged_edits_without_touching_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("committed\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    head_before = _git(repo, "rev-parse", "HEAD")

    (repo / "a.txt").write_text("unstaged edit\n")
    sha = temp_ref_of_working_tree(repo, "refs/mirror-tmp/test")

    assert _git(repo, "show", f"{sha}:a.txt") == "unstaged edit"
    assert _git(repo, "rev-parse", "refs/mirror-tmp/test") == sha
    assert _git(repo, "rev-parse", f"{sha}^") == head_before
    # HEAD, the index, and the working tree are untouched.
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "status", "--porcelain") == "M a.txt"
    assert (repo / "a.txt").read_text() == "unstaged edit\n"


def test_standalone_projects_use_the_rolling_supply_chain_cooldown() -> None:
    """Every standalone uv project must express the cooldown as a rolling window.

    A project excluded from the root workspace resolves against its own lock, so the
    root's cutoff does not constrain it at all -- and nothing advances a second copy
    of that cutoff: this script moves the root's and the mirror overlay's, and a
    standalone project is neither. A pinned timestamp there would start rotting the
    day it was written, silently widening that one project's supply-chain window. A
    relative window cannot: it is always the current policy. This holds the window
    equal to ``DEPENDENCY_COOLDOWN``, the single place the policy is stated, and
    requires the uv floor that makes it load-bearing -- uv below 0.10 does not reject
    a relative value, it drops the cooldown silently and discards the lockfile.

    This lives here rather than in the root ``test_meta_ratchets.py`` because that
    file is carried into the public mirror, where ``scripts/release.py`` is not: an
    import of it there fails at collection. The public tree has no standalone
    projects anyway (they are private apps), so nothing is lost by keeping the check
    on this side of the boundary.
    """
    expected_window = f"{int(DEPENDENCY_COOLDOWN.total_seconds() // 86400)} days"
    problems: list[str] = []
    for project_dir in iter_standalone_project_dirs():
        relative_dir = project_dir.relative_to(REPO_ROOT)
        project_uv = tomllib.loads((project_dir / "pyproject.toml").read_text()).get("tool", {}).get("uv", {})
        window = project_uv.get("exclude-newer")
        if window != expected_window:
            problems.append(f"{relative_dir}: [tool.uv] exclude-newer is {window!r}, expected {expected_window!r}")
        required_version = project_uv.get("required-version")
        if required_version is None or Version(str(required_version).lstrip(">=~^ ")) < Version("0.10"):
            problems.append(
                f"{relative_dir}: [tool.uv] required-version is {required_version!r}; "
                "a relative exclude-newer needs uv >= 0.10, which older uv drops silently"
            )
    assert not problems, "Standalone uv projects must declare the rolling supply-chain cooldown:\n" + "\n".join(
        f"  - {p}" for p in problems
    )


def test_mirrored_python_files_only_import_mirrored_scripts_modules() -> None:
    """A file the public mirror carries must not import a ``scripts`` module it does not.

    ``mirror/copy.bara.sky`` states this invariant in a comment ("The subset is
    import-self-contained") but nothing enforced it, so the failure mode was a green
    private test suite and a mirror gate that dies at collection: the public tree is
    materialized, ``test_meta_ratchets.py`` comes across, the module it imports does
    not, and every test in that file errors out. Checking it here turns a CI-only
    discovery into a local one.

    Only ``scripts`` imports are checked. ``libs/`` and ``apps/minds`` cross the
    boundary wholesale, so an import of those cannot dangle; the ``scripts`` subset is
    hand-picked file by file, which is what makes it easy to fall out of.
    """
    sky = (REPO_ROOT / "mirror" / "copy.bara.sky").read_text()
    include_block = sky[sky.index("include = [") : sky.index("exclude = [")]
    mirrored = set(re.findall(r'"([^"]+)"', include_block))

    dangling: list[str] = []
    for relative_path in sorted(mirrored):
        if not relative_path.endswith(".py"):
            continue
        source_file = REPO_ROOT / relative_path
        if not source_file.exists():
            continue
        tree = ast.parse(source_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("scripts."):
                imported = node.module
            elif isinstance(node, ast.Import) and any(a.name.startswith("scripts.") for a in node.names):
                imported = next(a.name for a in node.names if a.name.startswith("scripts."))
            else:
                continue
            imported_path = imported.replace(".", "/") + ".py"
            if imported_path not in mirrored:
                dangling.append(f"{relative_path} imports {imported}, which the mirror does not carry")

    assert not dangling, (
        "Mirrored files must be import-self-contained (see mirror/copy.bara.sky). Either add the "
        "module to PUBLIC_FILES or move the importing code out of the mirrored file:\n"
        + "\n".join(f"  - {d}" for d in dangling)
    )
