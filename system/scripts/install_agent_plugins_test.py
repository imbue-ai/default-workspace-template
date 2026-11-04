"""install_agent_plugins.sh puts each plugin's pinned commit in place as a local marketplace.

Local git repositories stand in for GitHub, served over file:// so a single commit can be
fetched by its hash the way GitHub allows.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).with_name("install_agent_plugins.sh")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for relative, text in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    return path


def _run(
    tmp_path: Path, guardian_commit: str, claude_code_commit: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        env={
            **os.environ,
            "CODE_GUARDIAN_REPO": (tmp_path / "code-guardian").as_uri(),
            "CODE_GUARDIAN_COMMIT": guardian_commit,
            "CLAUDE_CODE_REPO": (tmp_path / "claude-code").as_uri(),
            "CLAUDE_CODE_COMMIT": claude_code_commit,
            "AGENT_PLUGINS_ROOT": str(tmp_path / "plugins"),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _sources(tmp_path: Path) -> tuple[str, str, str]:
    """Two code-guardian commits and one claude-code commit; returns their hashes."""
    guardian = _repo(tmp_path / "code-guardian")
    first = _commit(
        guardian,
        {
            ".claude-plugin/marketplace.json": "{}",
            "plugins/imbue-code-guardian/VERSION": "1",
        },
        "first",
    )
    second = _commit(guardian, {"plugins/imbue-code-guardian/VERSION": "2"}, "second")
    claude_code = _repo(tmp_path / "claude-code")
    claude_code_commit = _commit(
        claude_code,
        {
            "plugins/frontend-design/skills/frontend-design/SKILL.md": "design",
            "plugins/other-plugin/README.md": "not wanted",
            "README.md": "the rest of the repo",
        },
        "plugins",
    )
    return first, second, claude_code_commit


def test_each_marketplace_is_put_in_place_at_its_pinned_commit(tmp_path: Path) -> None:
    first, _, claude_code_commit = _sources(tmp_path)

    result = _run(tmp_path, first, claude_code_commit)

    assert result.returncode == 0, result.stderr
    guardian = tmp_path / "plugins" / "imbue-code-guardian"
    assert (guardian / "plugins/imbue-code-guardian/VERSION").read_text() == "1"
    assert not (guardian / ".git").exists()
    pinned = tmp_path / "plugins" / "claude-code-plugins-pinned"
    assert sorted(path.name for path in (pinned / "plugins").iterdir()) == [
        "frontend-design"
    ]
    assert (
        pinned / "plugins/frontend-design/skills/frontend-design/SKILL.md"
    ).read_text() == "design"
    manifest = json.loads((pinned / ".claude-plugin/marketplace.json").read_text())
    assert manifest["name"] == "claude-code-plugins-pinned"
    assert manifest["plugins"] == [
        {"name": "frontend-design", "source": "./plugins/frontend-design"}
    ]
    assert sorted(path.name for path in (tmp_path / "plugins").iterdir()) == [
        "claude-code-plugins-pinned",
        "imbue-code-guardian",
    ]


def test_a_marketplace_already_at_its_pin_is_left_alone(tmp_path: Path) -> None:
    first, _, claude_code_commit = _sources(tmp_path)
    _run(tmp_path, first, claude_code_commit)
    shutil.rmtree(tmp_path / "code-guardian")
    shutil.rmtree(tmp_path / "claude-code")

    result = _run(tmp_path, first, claude_code_commit)

    assert result.returncode == 0, result.stderr


def test_a_pin_bump_replaces_the_marketplace(tmp_path: Path) -> None:
    first, second, claude_code_commit = _sources(tmp_path)
    _run(tmp_path, first, claude_code_commit)

    result = _run(tmp_path, second, claude_code_commit)

    assert result.returncode == 0, result.stderr
    guardian = tmp_path / "plugins" / "imbue-code-guardian"
    assert (guardian / "plugins/imbue-code-guardian/VERSION").read_text() == "2"
    assert (guardian / ".pinned-commit").read_text().strip() == second


def test_a_failed_fetch_fails_and_keeps_the_marketplace_in_place(
    tmp_path: Path,
) -> None:
    first, _, claude_code_commit = _sources(tmp_path)
    _run(tmp_path, first, claude_code_commit)

    result = _run(tmp_path, "0" * 40, claude_code_commit)

    assert result.returncode != 0
    guardian = tmp_path / "plugins" / "imbue-code-guardian"
    assert (guardian / "plugins/imbue-code-guardian/VERSION").read_text() == "1"
    assert not list((tmp_path / "plugins").glob(".staging.*"))
