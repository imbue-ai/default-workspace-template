"""What `build_template.sh` actually writes into a published snapshot.

The assembly script had no test at all, which is how a workspace-wiping hazard
survived in it. These run the real script over a real repo and assert on the
files a publisher ships, because every one of them is read by someone who is
not the publisher: the adopter's agent boots the generated `/welcome`, and a
human browsing GitHub reads the generated README.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parents[3]
_SCHEMA = (
    _REPO_ROOT / "system/services/env_converge/src/env_converge/template_manifest.py"
)
_RESOLVE_TEMPLATE_BASE = _REPO_ROOT / ".agents/shared/scripts/resolve_template_base.py"
# The scan is a hard gate with no fallback: both binaries are baked into the
# workspace image, so their absence means a dev box rather than a real failure.
_SCANNERS = ("betterleaks", "kingfisher")

_needs_scanners = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in _SCANNERS),
    reason=f"needs the workspace image's secret scanners ({', '.join(_SCANNERS)})",
)

# A test that assembles for real runs both scanners, two `uv run --no-project`
# resolutions and a boot smoke-check in one subprocess. `timeout_func_only`
# leaves the `built_snapshot` fixture's assembly unclocked, but a test that
# calls the script from its own body is clocked against the suite's 10s
# unit-test budget, which a loaded box does overrun. Only the tests that reach
# the scanners get this; the refusal tests return at exit 2/5 and keep the
# tight budget, where a hang is a real defect.
_REAL_ASSEMBLY_TIMEOUT_SECONDS = 180


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_source_repo(root: Path) -> tuple[Path, str]:
    """A minimal bootable workspace with one app to publish; returns (repo, base).

    The template is its own commit with bootstrap's `Initial workspace commit`
    on top, as in a real workspace; that marker is the base.
    """
    source = root / "source"
    source.mkdir()
    _git("init", "-q", ".", cwd=source)
    _git("config", "user.email", "t@t.t", cwd=source)
    _git("config", "user.name", "T", cwd=source)
    for relative in (
        "system",
        "system/supervisord.conf.d",
        ".agents/skills/welcome",
        "docs",
        ".agents/skills/publish-template/scripts",
        "system/services/env_converge/src/env_converge",
    ):
        (source / relative).mkdir(parents=True, exist_ok=True)
    (source / "pyproject.toml").write_text('[project]\nname="x"\n')
    # A base the assembly accepts as bootable: it must name the drop-in
    # directory, and the config it ships has to realize at least one program.
    # Programs live one per drop-in, so the main config only pulls them in.
    (source / "system/supervisord.conf").write_text(
        "[supervisord]\n\n[include]\nfiles = supervisord.conf.d/*.conf\n"
    )
    (source / "system/supervisord.conf.d/system_interface.conf").write_text(
        "[program:system_interface]\ncommand=bash -c 'system-interface'\n"
    )
    (source / "README.md").write_text("# base\n")
    (source / ".agents/skills/welcome/SKILL.md").write_text("base welcome\n")
    (source / "docs/VERSION_HISTORY.md").write_text("# V\n")
    (source / ".gitignore").write_text("data/*\n")
    for name in (
        "build_template.sh",
        "scan_secrets.sh",
        "betterleaks.toml",
        "validate_template.py",
        "write_template_manifest.py",
    ):
        shutil.copy(
            _SCRIPTS_DIR / name,
            source / ".agents/skills/publish-template/scripts" / name,
        )
    shutil.copy(_SCHEMA, source / "system/services/env_converge/src/env_converge")
    _git("add", "-A", cwd=source)
    _git("commit", "-qm", "Template release one", cwd=source)
    _git("commit", "-q", "--allow-empty", "-m", "Initial workspace commit", cwd=source)
    base_ref = _git("rev-parse", "HEAD", cwd=source)

    (source / "system/apps/demo").mkdir(parents=True)
    (source / "system/apps/demo/main.py").write_text("x = 1\n")
    # An MCP server the app relies on: included, it must ship as .mcp.template.json.
    (source / ".mcp.json").write_text(
        '{"mcpServers": {"demo": {"command": "demo-mcp", "args": []}}}\n'
    )
    _git("add", "-A", cwd=source)
    _git("commit", "-qm", "the app being published", cwd=source)
    return source, base_ref


def _update_self(source: Path, base_ref: str) -> None:
    """Build a second app, then land an update-self merge of a new template release.

    Afterwards the published app changes again, so it has work on both sides of
    the update -- the shape a user publishing one app out of several has.
    """
    (source / "system/apps/music_scout").mkdir(parents=True)
    (source / "system/apps/music_scout/main.py").write_text("scout = 1\n")
    _git("add", "-A", cwd=source)
    _git("commit", "-qm", "Build music scout", cwd=source)
    _git("checkout", "-q", "-b", "upstream", f"{base_ref}^", cwd=source)
    (source / "system/release_two.md").write_text("two\n")
    _git("add", "-A", cwd=source)
    _git("commit", "-qm", "Template release two", cwd=source)
    _git("checkout", "-q", "-", cwd=source)
    _git(
        "merge",
        "-q",
        "--no-ff",
        "upstream",
        "-m",
        "update-self: merge upstream template (minds-v0.0.2)",
        cwd=source,
    )
    (source / "system/apps/demo/main.py").write_text("x = 20\n")
    _git("add", "-A", cwd=source)
    _git("commit", "-qm", "Improve the app being published", cwd=source)


def _resolve_base(source: Path) -> str:
    return subprocess.run(
        [sys.executable, str(_RESOLVE_TEMPLATE_BASE), "--repo", str(source)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _linked_worktree(source: Path, root: Path) -> Path:
    """A throwaway linked worktree of the source's HEAD, where assembly may run."""
    worktree = root / "wt"
    _git("worktree", "add", "-q", str(worktree), "HEAD", cwd=source)
    return worktree


def _assemble(
    cwd: Path, base_ref: str, *extra: str, live_workspace: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the real script. `live_workspace` is where it reads data paths from."""
    env = dict(os.environ)
    if live_workspace is not None:
        env["ENV_CONVERGE_WORKSPACE_DIR"] = str(live_workspace)
    return subprocess.run(
        [
            "bash",
            ".agents/skills/publish-template/scripts/build_template.sh",
            "--base-ref",
            base_ref,
            "--slug",
            "demo",
            "--title",
            "Demo",
            "--description",
            "A demo.",
            "--include",
            "system/apps/demo",
            "--include",
            ".mcp.json",
            *extra,
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def built_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A template assembled by the real script, in a real linked worktree."""
    root = tmp_path_factory.mktemp("publish")
    source, base_ref = _make_source_repo(root)
    worktree = _linked_worktree(source, root)

    completed = _assemble(worktree, base_ref)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    return worktree


def test_assembly_refuses_to_run_outside_a_throwaway_worktree(tmp_path: Path) -> None:
    """The guard that exists because this once wiped a live workspace.

    Assembly resets the tree and runs `git clean -fdxq`, which deletes
    untracked AND gitignored files -- in a live mind that is `data/`, `.mngr/`,
    and the secrets. Run from a main worktree it must refuse before touching
    anything, rather than succeed and report a publish.
    """
    source, base_ref = _make_source_repo(tmp_path)
    (source / "data").mkdir()
    (source / "data/important.db").write_text("PRECIOUS USER DATA")

    completed = _assemble(source, base_ref)

    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "MAIN worktree" in completed.stderr
    assert (source / "data/important.db").read_text() == "PRECIOUS USER DATA"


@_needs_scanners
def test_an_included_mcp_config_ships_renamed_so_nothing_activates_before_its_secrets(
    built_snapshot: Path,
) -> None:
    assert not (built_snapshot / ".mcp.json").exists()
    assert '"demo-mcp"' in (built_snapshot / ".mcp.template.json").read_text()


@_needs_scanners
def test_the_manifest_trio_is_written(built_snapshot: Path) -> None:
    # The three files an adopter's tooling looks for. Absence of the TOML is
    # what marks a repo as the older v1 format, so a missing one is not a
    # cosmetic gap -- it silently changes how the template is read.
    assert (built_snapshot / "template.md").is_file()
    assert (built_snapshot / "template.toml").is_file()
    assert (built_snapshot / "template.svg").is_file()


@_needs_scanners
def test_the_readme_is_regenerated_to_describe_this_template(
    built_snapshot: Path,
) -> None:
    """The base README describes the generic workspace template, not this one.

    The repo's landing page is what decides whether anyone boots the thing, so
    assembly overwrites it wholesale rather than leaving the base text.
    """
    readme = (built_snapshot / "README.md").read_text()

    assert "# Demo" in readme
    assert "# base" not in readme
    # The repo does not exist yet, so the call-to-action carries a placeholder
    # the lead substitutes before the push; §8 blocks a push that still has it.
    assert "MINDS_TEMPLATE_REPO_URL" in readme


@_needs_scanners
def test_the_generated_welcome_replaces_the_base_one(built_snapshot: Path) -> None:
    # A mind created from a template must open by naming THAT template, not
    # with the generic greeting the base workspace ships.
    welcome = (built_snapshot / ".agents/skills/welcome/SKILL.md").read_text()

    assert "base welcome" not in welcome
    assert "Demo" in welcome
    assert "template.md" in welcome


@_needs_scanners
def test_the_version_history_never_ships(built_snapshot: Path) -> None:
    # docs/VERSION_HISTORY.md is the SOURCE workspace's ledger -- it records
    # what that mind published, which is nobody else's business and wrong in an
    # adopter's tree.
    assert not (built_snapshot / "docs/VERSION_HISTORY.md").exists()


def test_assembly_refuses_an_update_self_merge_as_the_base(tmp_path: Path) -> None:
    """The merge's tree and history hold the whole pre-update workspace.

    The script must refuse it before the reset, whatever the caller resolved.
    """
    source, base_ref = _make_source_repo(tmp_path)
    _update_self(source, base_ref)
    merge = _git("rev-parse", "HEAD^", cwd=source)
    worktree = _linked_worktree(source, tmp_path)

    completed = _assemble(worktree, merge)

    assert completed.returncode == 5, completed.stdout + completed.stderr
    assert "Initial workspace commit" in completed.stderr
    assert (worktree / "system/apps/music_scout/main.py").is_file()


@_needs_scanners
@pytest.mark.timeout(_REAL_ASSEMBLY_TIMEOUT_SECONDS)
def test_an_updated_workspace_publishes_only_the_selected_app(tmp_path: Path) -> None:
    source, base_ref = _make_source_repo(tmp_path)
    _update_self(source, base_ref)
    music_scout_commit = _git("rev-parse", "HEAD~2", cwd=source)
    resolved = _resolve_base(source)
    worktree = _linked_worktree(source, tmp_path)

    completed = _assemble(worktree, resolved)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (worktree / "system/apps/demo/main.py").read_text() == "x = 20\n"
    assert (worktree / "system/release_two.md").is_file()
    assert not (worktree / "system/apps/music_scout").exists()
    history = _git("rev-list", "HEAD", cwd=worktree).splitlines()
    assert music_scout_commit not in history


@_needs_scanners
@pytest.mark.timeout(_REAL_ASSEMBLY_TIMEOUT_SECONDS)
def test_a_mind_created_from_a_published_template_can_publish(tmp_path: Path) -> None:
    """Its history carries the source mind's Initial workspace commit too.

    The published snapshot is parented on the source's marker and the adopter
    clones it with full history, so only the adopter's own, newest marker may
    decide whether a base carries workspace work.
    """
    source, source_base = _make_source_repo(tmp_path)
    snapshot = _git(
        "commit-tree",
        "HEAD^{tree}",
        "-p",
        source_base,
        "-m",
        "template: demo",
        cwd=source,
    )
    _git("reset", "-q", "--hard", snapshot, cwd=source)
    _git("commit", "-q", "--allow-empty", "-m", "Initial workspace commit", cwd=source)
    (source / "system/apps/demo/main.py").write_text("x = 30\n")
    _git("add", "-A", cwd=source)
    _git("commit", "-qm", "Remix the adopted app", cwd=source)
    resolved = _resolve_base(source)
    worktree = _linked_worktree(source, tmp_path)

    completed = _assemble(worktree, resolved)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (worktree / "system/apps/demo/main.py").read_text() == "x = 30\n"


@_needs_scanners
@pytest.mark.timeout(_REAL_ASSEMBLY_TIMEOUT_SECONDS)
def test_an_opted_in_data_path_ships_from_the_live_workspace(tmp_path: Path) -> None:
    """The worker's worktree never has the data; only the live workspace does.

    That worktree is a `transfer = "git-worktree"` checkout, which carries no
    gitignored file, and all of `data/` is gitignored -- so the path has to be
    read from the live workspace, and then force-added past the same gitignore
    to reach the commit. Writing the data into the worktree instead would
    manufacture the one precondition production does not supply, and the test
    would pass over a snapshot that ships nothing.
    """
    source, base_ref = _make_source_repo(tmp_path)
    (source / "data/demo").mkdir(parents=True)
    (source / "data/demo/seed.json").write_text('{"rows": 1}\n')
    worktree = _linked_worktree(source, tmp_path)
    assert not (worktree / "data/demo").exists(), "the checkout must not carry data/"

    completed = _assemble(
        worktree, base_ref, "--data-include", "data/demo", live_workspace=source
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    shipped = _git("ls-tree", "-r", "--name-only", "HEAD", cwd=worktree).splitlines()
    assert "data/demo/seed.json" in shipped
