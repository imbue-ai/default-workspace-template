"""Pinned-version regression test for how Claude Code finds a skill an app carries.

An app the workspace built or installed keeps its skills in its own directory,
``system/apps/<package>/skills/<name>/``, and ``app-install`` makes each one
discoverable by generating a tracked symlink, ``.agents/skills/<name>``, whose
target is ``../../system/apps/<package>/skills/<name>``. Claude Code reads its
project skills from ``.claude/skills``, itself a link to ``.agents/skills``, so
it reaches such a skill through two links.

Nothing documents that Claude Code lists a skill whose entry is a symlink, and
the whole layout leans on it: if a version stopped following the link, every
skill an app carries would vanish from the agent without an error anywhere.
This test re-verifies it against the real ``claude`` binary, by reading the
skills the session reports when it starts. A skill left inside an app with no
link is not listed, which is what shows the link is what makes one found.

Marked ``real_claude``: it runs the real claude binary, against a local stand-in
for the API, with no network beyond loopback. Skipped when the binary is
missing or its version differs from the pin in .mngr/settings.toml, so it only
ever asserts against the exact binary workspaces ship. It does not run in CI.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.testing import StandInAnthropicApi
from imbue.chat.testing import skip_unless_pinned_claude

pytestmark = pytest.mark.real_claude

_RUN_TIMEOUT_SECONDS = 150.0
_APP_PACKAGE = "notes"
_PLAIN_SKILL = "tidy-desk"
_LINKED_APP_SKILL = "notes-add"
_UNLINKED_APP_SKILL = "notes-archive"


def _write_skill(directory: Path, name: str) -> None:
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Do the {name} thing.\n---\n\n# {name}\n\nDo the thing.\n"
    )


def _write_workspace_with_an_app_that_carries_skills(project_dir: Path) -> None:
    """A workspace tree as ``app-install`` leaves it: a skill of the workspace's own, an app that carries two
    skills in its directory, and the generated link to one of them."""
    skills_dir = project_dir / ".agents" / "skills"
    app_skills_dir = project_dir / "system" / "apps" / _APP_PACKAGE / "skills"
    _write_skill(skills_dir / _PLAIN_SKILL, _PLAIN_SKILL)
    _write_skill(app_skills_dir / _LINKED_APP_SKILL, _LINKED_APP_SKILL)
    _write_skill(app_skills_dir / _UNLINKED_APP_SKILL, _UNLINKED_APP_SKILL)
    (skills_dir / _LINKED_APP_SKILL).symlink_to(
        Path("..") / ".." / "system" / "apps" / _APP_PACKAGE / "skills" / _LINKED_APP_SKILL
    )
    (project_dir / ".claude").mkdir()
    (project_dir / ".claude" / "skills").symlink_to(Path("..") / ".agents" / "skills")
    subprocess.run(["git", "init", "-q", str(project_dir)], check=True, timeout=30)


def _write_config_dir(config_dir: Path, base_url: str) -> None:
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_API_KEY": "sk-ant-skill-discovery"}})
    )
    # Pre-dismiss first-run state so a headless -p run does not stall on a dialog.
    (config_dir / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "effortCalloutDismissed": True,
                "hasAcknowledgedCostThreshold": True,
                "bypassPermissionsModeAccepted": True,
            }
        )
    )


def _session_start_event(stdout: str) -> dict[str, Any]:
    """The event a ``--output-format stream-json`` session opens with, which names what it loaded."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
            return event
    raise AssertionError(f"claude -p reported no session start event; its output was:\n{stdout}")


@pytest.mark.timeout(180)
def test_claude_code_lists_a_skill_reached_through_the_link_app_install_generates(tmp_path: Path) -> None:
    skip_unless_pinned_claude()
    project_dir = tmp_path / "workspace"
    _write_workspace_with_an_app_that_carries_skills(project_dir)
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    config_dir = tmp_path / "claude-config"

    with StandInAnthropicApi(reply_for=lambda _body: "OK") as server:
        _write_config_dir(config_dir, server.base_url)
        completed = subprocess.run(
            [
                "claude",
                "-p",
                "Reply with exactly: OK",
                "--model",
                "claude-haiku-4-5",
                "--output-format",
                "stream-json",
                "--verbose",
            ],
            env={
                "HOME": str(isolated_home),
                "PATH": os.environ["PATH"],
                "CLAUDE_CONFIG_DIR": str(config_dir),
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
            cwd=project_dir,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT_SECONDS,
        )

    listed_skills = _session_start_event(completed.stdout)["skills"]
    # The workspace's own skill shows the listing reads this tree at all.
    assert _PLAIN_SKILL in listed_skills
    assert _LINKED_APP_SKILL in listed_skills
    assert _UNLINKED_APP_SKILL not in listed_skills
