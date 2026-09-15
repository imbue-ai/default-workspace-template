"""What `build_template.sh` actually writes into a published snapshot.

The assembly script had no test at all, which is how a workspace-wiping hazard
survived in it. These run the real script over a real repo and assert on the
files a publisher ships, because every one of them is read by someone who is
not the publisher: the adopter's agent boots the generated `/welcome`, and a
human browsing GitHub reads the generated README.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parents[3]
_SCHEMA = (
    _REPO_ROOT / "system/services/env_converge/src/env_converge/template_manifest.py"
)
# The scan is a hard gate with no fallback: both binaries are baked into the
# workspace image, so their absence means a dev box rather than a real failure.
_SCANNERS = ("betterleaks", "kingfisher")

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in _SCANNERS),
    reason=f"needs the workspace image's secret scanners ({', '.join(_SCANNERS)})",
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_source_repo(root: Path) -> tuple[Path, str]:
    """A minimal bootable workspace with one app to publish; returns (repo, base)."""
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
    _git("commit", "-qm", "Initial workspace commit", cwd=source)
    base_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    (source / "system/apps/demo").mkdir(parents=True)
    (source / "system/apps/demo/main.py").write_text("x = 1\n")
    _git("add", "-A", cwd=source)
    _git("commit", "-qm", "the app being published", cwd=source)
    return source, base_ref


def _assemble(cwd: Path, base_ref: str) -> subprocess.CompletedProcess[str]:
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
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def built_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A template assembled by the real script, in a real linked worktree."""
    root = tmp_path_factory.mktemp("publish")
    source, base_ref = _make_source_repo(root)
    worktree = root / "wt"
    _git("worktree", "add", "-q", str(worktree), "HEAD", cwd=source)

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


def test_the_manifest_trio_is_written(built_snapshot: Path) -> None:
    # The three files an adopter's tooling looks for. Absence of the TOML is
    # what marks a repo as the older v1 format, so a missing one is not a
    # cosmetic gap -- it silently changes how the template is read.
    assert (built_snapshot / "template.md").is_file()
    assert (built_snapshot / "template.toml").is_file()
    assert (built_snapshot / "template.svg").is_file()


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


def test_the_generated_welcome_replaces_the_base_one(built_snapshot: Path) -> None:
    # A mind created from a template must open by naming THAT template, not
    # with the generic greeting the base workspace ships.
    welcome = (built_snapshot / ".agents/skills/welcome/SKILL.md").read_text()

    assert "base welcome" not in welcome
    assert "Demo" in welcome
    assert "template.md" in welcome


def test_the_version_history_never_ships(built_snapshot: Path) -> None:
    # docs/VERSION_HISTORY.md is the SOURCE workspace's ledger -- it records
    # what that mind published, which is nobody else's business and wrong in an
    # adopter's tree.
    assert not (built_snapshot / "docs/VERSION_HISTORY.md").exists()
