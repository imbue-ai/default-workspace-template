"""Pinned-version regression tests for Claude Code settings-env auth behaviors.

The whole settings-env auth design leans on Claude Code behaviors that were
verified empirically against the pinned binary and are NOT all documented --
two of them contradict the official docs. These tests re-verify them against
the real `claude` binary so a version bump that changes any of them fails
loudly here instead of silently breaking workspace auth:

1. A credential defined in the settings.json `env` block drives provider
   selection (`ANTHROPIC_API_KEY` -> api_key auth, `CLAUDE_CODE_OAUTH_TOKEN`
   -> oauth_token auth).
2. The settings-env credential is actually SENT on API requests, and the
   settings-env `ANTHROPIC_BASE_URL` is honored (requests hit the override).
3. A settings-env credential OVERRIDES a conflicting process-env value
   (docs claim shell env wins; the pinned binary does the opposite, and the
   migration story depends on it -- a stale host-env key must not shadow a
   settings-managed one).
4. An `ANTHROPIC_API_KEY` outranks a `CLAUDE_CODE_OAUTH_TOKEN` when both are
   present (why the paste parser rejects mixed-mode pastes).
5. `env` maps deep-merge across settings scopes (a project-level
   `.claude/settings.json` env key composes with the user-level block).

Marked `real_claude`: they run the real claude binary (no network beyond loopback
-- a local capture server plays the API and answers 401, which is enough to
observe the credential headers). Skipped when the binary is missing or its
version differs from the pin in .mngr/settings.toml, so they only ever
assert against the exact binary workspaces ship.
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

_CAPTURE_WAIT_SECONDS = 60.0


def _write_config_dir(tmp_path: Path, settings_env: dict[str, str]) -> Path:
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "settings.json").write_text(json.dumps({"env": settings_env}))
    # Pre-dismiss first-run state so headless -p runs don't stall on dialogs
    # (mirrors what mngr provisioning writes for real agents).
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
    return config_dir


def _run_claude_p_until_captured(
    server: StandInAnthropicApi,
    config_dir: Path,
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Run `claude -p` against the capture server and return the first captured request."""
    isolated_home = tmp_path / "home"
    isolated_home.mkdir(exist_ok=True)
    run_cwd = cwd if cwd is not None else tmp_path / "cwd"
    run_cwd.mkdir(exist_ok=True)
    env = {
        "HOME": str(isolated_home),
        "PATH": os.environ["PATH"],
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        **(extra_env or {}),
    }
    process = subprocess.Popen(
        ["claude", "-p", "Reply with exactly: OK", "--model", "claude-haiku-4-5"],
        env=env,
        cwd=run_cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Event-driven wait (short slices so an early subprocess exit is
        # noticed promptly) rather than sleep-polling.
        waited = 0.0
        while waited < _CAPTURE_WAIT_SECONDS:
            if server.first_request_event.wait(timeout=0.2):
                return server.captured[0]
            waited += 0.2
            if process.poll() is not None and not server.first_request_event.is_set():
                raise AssertionError("claude -p exited without ever contacting the capture server")
        raise AssertionError("claude -p never contacted the capture server within the wait window")
    finally:
        process.kill()
        process.wait(timeout=30)


def _auth_status(config_dir: Path, tmp_path: Path) -> dict[str, object]:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir(exist_ok=True)
    env = {
        "HOME": str(isolated_home),
        "PATH": os.environ["PATH"],
        "CLAUDE_CONFIG_DIR": str(config_dir),
    }
    result = subprocess.run(
        ["claude", "auth", "status", "--json"], env=env, capture_output=True, text=True, timeout=60
    )
    return json.loads(result.stdout)


@pytest.mark.timeout(120)
def test_settings_env_api_key_drives_provider_selection(tmp_path: Path) -> None:
    skip_unless_pinned_claude()
    config_dir = _write_config_dir(tmp_path, {"ANTHROPIC_API_KEY": "sk-ant-settings-selection"})
    status = _auth_status(config_dir, tmp_path)
    assert status["loggedIn"] is True
    assert status["authMethod"] == "api_key"


@pytest.mark.timeout(120)
def test_settings_env_oauth_token_drives_provider_selection(tmp_path: Path) -> None:
    skip_unless_pinned_claude()
    config_dir = _write_config_dir(tmp_path, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-settings-token"})
    status = _auth_status(config_dir, tmp_path)
    assert status["loggedIn"] is True
    assert status["authMethod"] == "oauth_token"


@pytest.mark.timeout(180)
def test_settings_env_key_and_base_url_reach_the_request_and_beat_shell_env(tmp_path: Path) -> None:
    """Behaviors 2 + 3 in one run: the request hits the settings base URL
    carrying the settings key, even with a conflicting shell-env key."""
    skip_unless_pinned_claude()
    with StandInAnthropicApi() as server:
        config_dir = _write_config_dir(
            tmp_path,
            {"ANTHROPIC_BASE_URL": server.base_url, "ANTHROPIC_API_KEY": "sk-ant-SETTINGS-VALUE"},
        )
        captured = _run_claude_p_until_captured(
            server, config_dir, tmp_path, extra_env={"ANTHROPIC_API_KEY": "sk-ant-SHELL-VALUE"}
        )
    assert captured["x-api-key"] == "sk-ant-SETTINGS-VALUE"


@pytest.mark.timeout(180)
def test_settings_env_api_key_outranks_oauth_token(tmp_path: Path) -> None:
    skip_unless_pinned_claude()
    with StandInAnthropicApi() as server:
        config_dir = _write_config_dir(
            tmp_path,
            {
                "ANTHROPIC_BASE_URL": server.base_url,
                "ANTHROPIC_API_KEY": "sk-ant-BOTH-KEY",
                "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-BOTH-TOKEN",
            },
        )
        captured = _run_claude_p_until_captured(server, config_dir, tmp_path)
    assert captured["x-api-key"] == "sk-ant-BOTH-KEY"
    assert captured["authorization"] is None


@pytest.mark.timeout(180)
def test_settings_env_oauth_token_sent_as_bearer(tmp_path: Path) -> None:
    skip_unless_pinned_claude()
    with StandInAnthropicApi() as server:
        config_dir = _write_config_dir(
            tmp_path,
            {"ANTHROPIC_BASE_URL": server.base_url, "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-BEARER-ME"},
        )
        captured = _run_claude_p_until_captured(server, config_dir, tmp_path)
    assert captured["x-api-key"] is None
    assert captured["authorization"] == "Bearer sk-ant-oat01-BEARER-ME"


@pytest.mark.timeout(180)
def test_env_maps_deep_merge_across_settings_scopes(tmp_path: Path) -> None:
    """A project-layer base URL composes with a user-layer key (per-key merge)."""
    skip_unless_pinned_claude()
    with StandInAnthropicApi() as server:
        config_dir = _write_config_dir(tmp_path, {"ANTHROPIC_API_KEY": "sk-ant-USERLAYER-KEY"})
        project_dir = tmp_path / "project"
        (project_dir / ".claude").mkdir(parents=True)
        (project_dir / ".claude" / "settings.json").write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": server.base_url}})
        )
        subprocess.run(["git", "init", "-q", str(project_dir)], check=True, timeout=30)
        captured = _run_claude_p_until_captured(server, config_dir, tmp_path, cwd=project_dir)
    assert captured["x-api-key"] == "sk-ant-USERLAYER-KEY"
