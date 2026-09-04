"""The plugin skills must be available in the FIRST claude session started from a
directory that has never seen one -- the situation every launched worker is in.

This is the minimal reproduction of the crystallize-worker failure where
`/imbue-code-guardian:autofix` came back as ``Unknown skill``: Claude Code resolves
the plugin set at session startup, so a plugin that is only installed by the
SessionStart hook (from inside the session) is not loaded until the next session.
The worker template therefore runs ``claude_update_plugin.sh`` as a provision
command before the worker's claude starts; this test drives that exact sequence
against the real ``claude`` CLI in an isolated config dir and checks the skills are
there in session one. (The test passes ``--strict`` so an install failure is its
own clear assertion rather than a puzzling missing-skill result; the template
deliberately does not, so a plugin outage never blocks a worker launch.)

Real dependencies: the ``claude`` binary, network access to the plugin marketplace,
and an ``ANTHROPIC_API_KEY`` for the one-turn session (an isolated
``CLAUDE_CONFIG_DIR`` has no other credential source). Skipped when any is absent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_SCRIPT = _REPO_ROOT / "system" / "scripts" / "claude_update_plugin.sh"
_EXPECTED_SKILL = "imbue-code-guardian:autofix"
_SKILL_LIST_PROMPT = (
    "Do not use any tools. Reply with only the exact names of the skills available to your "
    "Skill tool whose names start with 'imbue-code-guardian:' -- one per line. "
    "If there are none, reply exactly NONE."
)


def _requires_real_claude() -> None:
    if shutil.which("claude") is None:
        pytest.skip("the claude CLI is not installed")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip(
            "ANTHROPIC_API_KEY is not set; an isolated config dir has no other credential"
        )


@pytest.fixture
def isolated_config_dir(tmp_path: Path) -> Path:
    """An empty claude config dir: no marketplace, no plugin, no session history. The
    provision step has to build everything the first session needs from here."""
    config_dir = tmp_path / f"claude-config-{uuid4().hex}"
    config_dir.mkdir()
    return config_dir


@pytest.fixture
def fresh_worktree(tmp_path: Path) -> Iterator[Path]:
    """A worktree of this repo at HEAD in a path no claude session has seen, as
    ``mngr create -t worker`` makes for a worker."""
    worktree = tmp_path / f"worker-{uuid4().hex}"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        yield worktree
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )


def _session_env(config_dir: Path, home: Path) -> dict[str, str]:
    """The session's environment: the isolated config dir, and a throwaway HOME so
    the repo's SessionStart hooks (which symlink ``tk`` into ``~/.local/bin`` and
    touch other per-user state) never rewrite the real user's home. uv's cache is
    kept so the hooks' ``uv sync`` stays warm."""
    uv_cache_dir = subprocess.run(
        ["uv", "cache", "dir"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return {
        **os.environ,
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "HOME": str(home),
        "UV_CACHE_DIR": uv_cache_dir,
    }


def _first_session_skill_list(worktree: Path, config_dir: Path, home: Path) -> str:
    result = subprocess.run(
        [
            "claude",
            "-p",
            "--model",
            "haiku",
            "--output-format",
            "json",
            "--max-turns",
            "1",
            _SKILL_LIST_PROMPT,
        ],
        cwd=worktree,
        env=_session_env(config_dir, home),
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )
    return str(json.loads(result.stdout)["result"])


@pytest.mark.acceptance
# A marketplace clone, a plugin install, and a one-turn session whose SessionStart
# hooks run `uv sync` in a fresh worktree: well past the root's 10s default.
@pytest.mark.timeout(900)
def test_provision_time_install_makes_plugin_skills_available_in_the_first_session(
    isolated_config_dir: Path, fresh_worktree: Path, tmp_path: Path
) -> None:
    _requires_real_claude()
    home = tmp_path / "home"
    home.mkdir()

    provision = subprocess.run(
        ["bash", str(_PLUGIN_SCRIPT), "--strict"],
        cwd=fresh_worktree,
        env=_session_env(isolated_config_dir, home),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert provision.returncode == 0, provision.stdout + provision.stderr

    skills = _first_session_skill_list(fresh_worktree, isolated_config_dir, home)

    assert _EXPECTED_SKILL in skills, skills
