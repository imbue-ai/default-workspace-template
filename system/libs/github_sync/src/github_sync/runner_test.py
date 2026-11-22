"""Unit tests for the github-sync watchdog runner.

Covers the visibility re-check policy (a confirmed answer is cached for the
check interval; a failed re-check keeps the last confirmed answer and retries
next tick) and full ticks: the status mirror the post-commit hook reads must
allow pushes only once the repo is confirmed private, hold while visibility is
unknown, halt when the repo is public, and the tick must re-apply the gateway
git wiring so the hook's pushes keep working across gateway restarts.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from github_sync.runner import (
    _do_tick,
    _refresh_visibility,
    _SyncState,
    status_file_path,
)
from github_sync.testing import (
    add_owned_app,
    git_output,
    init_bare_repo,
    init_workspace,
    install_fake_latchkey,
    link_app,
)
from github_sync.visibility import (
    VISIBILITY_CHECK_INTERVAL_SECONDS,
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
)

_REPO_URL = "https://github.com/some-user/my-workspace"

_GATEWAY_ENV = {
    "LATCHKEY_GATEWAY": "http://127.0.0.1:39999",
    "LATCHKEY_GATEWAY_PASSWORD": "gw-password",
}


def _write_sync_config(workspace: Path, repo_url: str = _REPO_URL) -> None:
    config_path = workspace / "data" / "system" / "github_sync.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(f'repo_url = "{repo_url}"\n')


def _set_gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _GATEWAY_ENV.items():
        monkeypatch.setenv(name, value)


def _global_git_config() -> str:
    result = subprocess.run(
        ["git", "config", "--global", "--list"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def test_do_tick_allows_pushes_when_repo_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    fake_latchkey_bin: Path,
) -> None:
    """A confirmed-private repo flips the status mirror to is_push_allowed=true
    (the post-commit hook does the actual pushing)."""
    monkeypatch.chdir(tmp_path)
    _write_sync_config(tmp_path)
    _set_gateway_env(monkeypatch)
    install_fake_latchkey(fake_latchkey_bin, "echo '{\"private\": true}'")
    state = _SyncState()

    _do_tick(state)

    status = json.loads(status_file_path().read_text())
    assert status["is_push_allowed"] is True
    assert status["visibility"] == "private"


def test_do_tick_reapplies_gateway_wiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    fake_latchkey_bin: Path,
) -> None:
    """Each tick re-installs the gateway rewrite in global git config, so the
    post-commit hook keeps pushing after a gateway port change."""
    monkeypatch.chdir(tmp_path)
    _write_sync_config(tmp_path)
    _set_gateway_env(monkeypatch)
    install_fake_latchkey(fake_latchkey_bin, "echo '{\"private\": true}'")

    _do_tick(_SyncState())

    config_text = _global_git_config()
    assert "insteadof=https://github.com/" in config_text
    assert "gw-password" in config_text


def test_do_tick_reports_incomplete_gateway_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
) -> None:
    """Without the latchkey env the tick cannot wire git; the status mirror
    must say so instead of silently reporting a healthy service."""
    monkeypatch.chdir(tmp_path)
    _write_sync_config(tmp_path)
    state = _SyncState()

    _do_tick(state)

    assert state.last_error is not None
    assert "wiring" in state.last_error
    status = json.loads(status_file_path().read_text())
    assert "wiring" in status["last_error"]


def test_do_tick_halts_pushes_when_repo_public(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    fake_latchkey_bin: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_sync_config(tmp_path)
    _set_gateway_env(monkeypatch)
    install_fake_latchkey(fake_latchkey_bin, "echo '{\"private\": false}'")
    state = _SyncState()

    _do_tick(state)

    assert state.is_push_allowed is False
    status = json.loads(status_file_path().read_text())
    assert status["is_push_allowed"] is False
    assert status["visibility"] == "public"


def test_do_tick_holds_pushes_while_visibility_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    fake_latchkey_bin: Path,
) -> None:
    """An unreachable visibility check (gateway offline) must fail closed."""
    monkeypatch.chdir(tmp_path)
    _write_sync_config(tmp_path)
    _set_gateway_env(monkeypatch)
    install_fake_latchkey(fake_latchkey_bin, "exit 7")

    _do_tick(_SyncState())

    status = json.loads(status_file_path().read_text())
    assert status["is_push_allowed"] is False
    assert status["visibility"] == "unknown"


def _confirmed_private_state(age_seconds: float) -> _SyncState:
    """A state whose private confirmation is `age_seconds` old."""
    state = _SyncState()
    state.repo_url = _REPO_URL
    state.visibility = VISIBILITY_PRIVATE
    state.visibility_checked_at = datetime.now(timezone.utc) - timedelta(
        seconds=age_seconds
    )
    return state


def test_refresh_visibility_skips_recheck_while_confirmed_answer_is_fresh(
    isolated_git_and_gateway_env: Path, fake_latchkey_bin: Path
) -> None:
    """A confirmed answer younger than the check interval must be trusted
    without re-asking GitHub (the fake would answer public, so any re-check
    would flip the state and fail the assertion)."""
    install_fake_latchkey(fake_latchkey_bin, "echo '{\"private\": false}'")
    state = _confirmed_private_state(age_seconds=1)
    checked_at_before = state.visibility_checked_at

    _refresh_visibility(state, _REPO_URL)

    assert state.visibility == VISIBILITY_PRIVATE
    assert state.is_push_allowed is True
    assert state.visibility_checked_at == checked_at_before


def test_refresh_visibility_keeps_last_confirmed_answer_when_recheck_fails(
    isolated_git_and_gateway_env: Path, fake_latchkey_bin: Path
) -> None:
    """A stale confirmation whose re-check fails outright (gateway offline)
    must keep the last confirmed answer -- pushes would fail too in that
    state, so halting adds nothing -- and leave checked_at unchanged so the
    re-check is retried on the next tick rather than in 15 minutes."""
    install_fake_latchkey(fake_latchkey_bin, "exit 7")
    state = _confirmed_private_state(age_seconds=VISIBILITY_CHECK_INTERVAL_SECONDS + 60)
    checked_at_before = state.visibility_checked_at

    _refresh_visibility(state, _REPO_URL)

    assert state.visibility == VISIBILITY_PRIVATE
    assert state.is_push_allowed is True
    assert state.visibility_checked_at == checked_at_before


def test_refresh_visibility_rechecks_stale_answer_and_halts_on_public(
    isolated_git_and_gateway_env: Path, fake_latchkey_bin: Path
) -> None:
    """Once the confirmed answer goes stale, a re-check happens and a repo
    that flipped public halts pushes."""
    install_fake_latchkey(fake_latchkey_bin, "echo '{\"private\": false}'")
    state = _confirmed_private_state(age_seconds=VISIBILITY_CHECK_INTERVAL_SECONDS + 60)
    checked_at_before = state.visibility_checked_at
    assert checked_at_before is not None

    _refresh_visibility(state, _REPO_URL)

    assert state.visibility == VISIBILITY_PUBLIC
    assert state.is_push_allowed is False
    assert state.visibility_checked_at is not None
    assert state.visibility_checked_at > checked_at_before


def test_do_tick_rechecks_visibility_when_repo_url_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    fake_latchkey_bin: Path,
) -> None:
    """A repointed github_sync.toml must not inherit the previous repo's
    confirmed-private answer: the new repo has to earn its own confirmation
    before any push (here the re-check fails, so pushes stay held)."""
    monkeypatch.chdir(tmp_path)
    _write_sync_config(tmp_path)
    _set_gateway_env(monkeypatch)
    install_fake_latchkey(fake_latchkey_bin, "exit 7")
    # State carried over from ticks against a different, previously-configured
    # repo, with a still-fresh private confirmation.
    state = _confirmed_private_state(age_seconds=1)
    state.repo_url = "https://github.com/some-user/previous-repo"

    _do_tick(state)

    assert state.repo_url == _REPO_URL
    status = json.loads(status_file_path().read_text())
    assert status["is_push_allowed"] is False
    assert status["visibility"] == "unknown"


def test_do_tick_idles_when_sync_not_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    state = _SyncState()
    _do_tick(state)

    assert state.repo_url is None
    status = json.loads(status_file_path().read_text())
    assert status["repo_url"] is None


def test_do_tick_reports_malformed_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "data" / "system" / "github_sync.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("repo_url = [broken")

    state = _SyncState()
    _do_tick(state)

    assert state.last_error is not None
    assert "github_sync.toml" in state.last_error


def test_do_tick_runs_the_app_pass_and_an_app_that_fails_stops_nothing_else(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    fake_latchkey_bin: Path,
) -> None:
    """Each tick pushes every linked app to its dev repository and names each
    app in the status mirror; an app whose push fails leaves the workspace's
    own sync, and the other apps, as they were."""
    workspace = init_workspace(tmp_path / "workspace")
    dev_repository = init_bare_repo(tmp_path / "notes-dev.git")
    notes_url = "https://github.com/some-user/notes-dev"
    link_app(workspace, add_owned_app(workspace, "notes", dev_repo_url=notes_url))
    # No gateway listens for this one, so its push fails.
    link_app(
        workspace,
        add_owned_app(
            workspace, "shelf", dev_repo_url="https://github.com/some-user/shelf-dev"
        ),
    )
    add_owned_app(workspace, "drafts")
    monkeypatch.chdir(workspace)
    _write_sync_config(workspace)
    _set_gateway_env(monkeypatch)
    # No test reaches a gateway: the one dev repository resolves to a local one.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{dev_repository}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", notes_url)
    install_fake_latchkey(fake_latchkey_bin, "echo '{\"private\": true}'")
    state = _SyncState()

    _do_tick(state)

    status = json.loads(status_file_path().read_text())
    assert status["is_push_allowed"] is True
    assert status["app_pass_error"] is None
    apps = {app["name"]: app for app in status["apps"]}
    assert {name: app["state"] for name, app in apps.items()} == {
        "drafts": "no_dev_repository",
        "notes": "pushed",
        "shelf": "failed",
    }
    assert apps["notes"]["pushed_commit"] == git_output(
        dev_repository, "rev-parse", "refs/heads/main"
    )
    assert apps["notes"]["visibility"] == "private"
    assert "shelf-dev" in apps["shelf"]["detail"]

    _do_tick(state)

    status = json.loads(status_file_path().read_text())
    assert {app["name"]: app["state"] for app in status["apps"]} == {
        "drafts": "no_dev_repository",
        "notes": "up_to_date",
        "shelf": "failed",
    }


@pytest.mark.parametrize("is_sync_configured", [False, True])
def test_do_tick_pushes_no_app_while_the_workspace_is_not_synced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    is_sync_configured: bool,
) -> None:
    """The app pass belongs to a workspace whose sync is on and wired: with no
    sync config, or with one and no gateway to push through, a linked app that
    could be pushed is left alone and the status lists no apps."""
    workspace = init_workspace(tmp_path / "workspace")
    dev_repository = init_bare_repo(tmp_path / "notes-dev.git")
    link_app(
        workspace, add_owned_app(workspace, "notes", dev_repo_url=str(dev_repository))
    )
    monkeypatch.chdir(workspace)
    if is_sync_configured:
        _write_sync_config(workspace)

    _do_tick(_SyncState())

    assert git_output(dev_repository, "for-each-ref") == ""
    status = json.loads(status_file_path().read_text())
    assert status["apps"] == []
    assert status["is_push_allowed"] is False


def test_do_tick_says_why_the_app_pass_did_not_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    fake_latchkey_bin: Path,
) -> None:
    """A service started somewhere that is not the workspace's checkout still
    watches the sync repo; the status says the apps were not looked at."""
    monkeypatch.chdir(tmp_path)
    _write_sync_config(tmp_path)
    _set_gateway_env(monkeypatch)
    install_fake_latchkey(fake_latchkey_bin, "echo '{\"private\": true}'")

    _do_tick(_SyncState())

    status = json.loads(status_file_path().read_text())
    assert status["is_push_allowed"] is True
    assert status["apps"] == []
    assert "not a git checkout" in status["app_pass_error"]
