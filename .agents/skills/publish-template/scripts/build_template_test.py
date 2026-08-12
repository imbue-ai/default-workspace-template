"""What `build_template.sh` actually writes into a published snapshot.

The assembly script had no test at all, which is how a hazard and a missing
licence both survived in it. These run the real script over a real repo and
assert on the files a publisher ships, because every one of them is read by
someone who is not the publisher: the adopter's agent boots the generated
`/welcome`, and a human browsing GitHub reads the generated README.
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


@pytest.fixture(scope="module")
def built_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A template assembled by the real script, in a real linked worktree."""
    root = tmp_path_factory.mktemp("publish")
    source = root / "source"
    source.mkdir()

    _git("init", "-q", ".", cwd=source)
    _git("config", "user.email", "t@t.t", cwd=source)
    _git("config", "user.name", "T", cwd=source)
    for relative in (
        "system",
        ".agents/skills/welcome",
        "docs",
        ".agents/skills/publish-template/scripts",
        "system/services/env_converge/src/env_converge",
    ):
        (source / relative).mkdir(parents=True, exist_ok=True)
    (source / "pyproject.toml").write_text('[project]\nname="x"\n')
    (source / "system/supervisord.conf").write_text("[supervisord]\n")
    (source / "README.md").write_text("# base\n")
    (source / ".agents/skills/welcome/SKILL.md").write_text("base welcome\n")
    (source / "docs/VERSION_HISTORY.md").write_text("# V\n")
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

    worktree = root / "wt"
    _git("worktree", "add", "-q", str(worktree), "HEAD", cwd=source)
    completed = subprocess.run(
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
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return worktree


def test_the_generated_welcome_tells_the_adopter_about_the_licence(
    built_snapshot: Path,
) -> None:
    """The first thing a mind made from a template says should include this.

    A published template is someone else's code landing in someone else's mind.
    The welcome is the one moment guaranteed to happen before they build on it,
    so the licence belongs there rather than only in a file they might open.
    """
    welcome = (built_snapshot / ".agents/skills/welcome/SKILL.md").read_text()

    assert "LICENSE" in welcome
    assert "no reuse rights" in welcome
    # It must read the repo's own file rather than trust a string baked in at
    # publish time, so it stays true for a template licensed some other way.
    assert "Read" in welcome


def test_the_licence_step_runs_before_the_turn_ends(built_snapshot: Path) -> None:
    """Ordering is load-bearing: one step closes the turn on a question.

    A licence step placed after it would simply never be reached in the first
    response, which is the whole point of putting it in the welcome.
    """
    welcome = (built_snapshot / ".agents/skills/welcome/SKILL.md").read_text()

    assert welcome.index("LICENSE") < welcome.index("End your first response")


def test_the_readme_carries_a_licence_section_awaiting_the_users_answer(
    built_snapshot: Path,
) -> None:
    # Assembly runs before the skill asks, so the section must exist and must
    # be unmistakably unfinished -- validate_template.py fails on the token.
    readme = (built_snapshot / "README.md").read_text()

    assert "## License" in readme
    assert "MINDS_TEMPLATE_LICENSE" in readme


def test_assembly_writes_no_licence_of_its_own(built_snapshot: Path) -> None:
    # Choosing a licence is the user's call, made in the skill's confirmation
    # step. A LICENSE that appeared without being asked for would be the script
    # deciding it for them.
    assert not (built_snapshot / "LICENSE").exists()
