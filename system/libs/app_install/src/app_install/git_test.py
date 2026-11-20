from pathlib import Path

import pytest

from app_install.conftest import RepositoryHostStandIn
from app_install.errors import GitCommandError
from app_install.errors import RepositoryUnreachableError
from app_install.git import fetch_commit
from app_install.git import land_commit
from app_install.git import run_git
from app_install.testing import commit_all
from app_install.testing import make_workspace_repository
from app_install.testing import write_workspace_file

_GATEWAY = "http://127.0.0.1:41234"
_APP_URL = "https://github.com/someone/notes"


def _set_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LATCHKEY_GATEWAY", _GATEWAY)
    monkeypatch.setenv("LATCHKEY_GATEWAY_PASSWORD", "pw-abc")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "jwt-xyz")


def test_a_read_with_the_workspaces_access_goes_through_the_gateway_with_its_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub sync need not have wired the container's global configuration: the command carries the wiring."""
    workspace = make_workspace_repository(tmp_path / "workspace")
    _set_gateway(monkeypatch)

    resolved = run_git(workspace, ["ls-remote", "--get-url", _APP_URL], is_gateway_used=True)
    headers = run_git(workspace, ["config", "--get-all", f"http.{_GATEWAY}/.extraHeader"], is_gateway_used=True)

    assert resolved == f"{_GATEWAY}/gateway/{_APP_URL}"
    assert headers.splitlines() == [
        "X-Latchkey-Gateway-Password: pw-abc",
        "X-Latchkey-Gateway-Permissions-Override: jwt-xyz",
    ]


def test_the_gateway_wiring_is_added_after_configuration_the_environment_already_carries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    _set_gateway(monkeypatch)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "installer.probe")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "kept")

    assert run_git(workspace, ["config", "--get", "installer.probe"], is_gateway_used=True) == "kept"
    assert run_git(workspace, ["ls-remote", "--get-url", _APP_URL], is_gateway_used=True).startswith(_GATEWAY)


def test_wiring_github_sync_already_wrote_is_not_sent_a_second_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    _set_gateway(monkeypatch)
    # What GitHub sync leaves in the container's global configuration once it is enabled.
    global_configuration = tmp_path / "gitconfig"
    global_configuration.write_text(
        f'[url "{_GATEWAY}/gateway/https://github.com/"]\n'
        "\tinsteadOf = https://github.com/\n"
        f'[http "{_GATEWAY}/"]\n'
        "\textraHeader = X-Latchkey-Gateway-Password: pw-abc\n"
        "\textraHeader = X-Latchkey-Gateway-Permissions-Override: jwt-xyz\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_configuration))

    headers = run_git(workspace, ["config", "--get-all", f"http.{_GATEWAY}/.extraHeader"], is_gateway_used=True)

    assert headers.splitlines() == [
        "X-Latchkey-Gateway-Password: pw-abc",
        "X-Latchkey-Gateway-Permissions-Override: jwt-xyz",
    ]
    assert run_git(workspace, ["ls-remote", "--get-url", _APP_URL], is_gateway_used=True).startswith(_GATEWAY)


def test_an_anonymous_read_and_a_local_command_leave_the_gateway_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    _set_gateway(monkeypatch)

    assert run_git(workspace, ["ls-remote", "--get-url", _APP_URL], is_global_config_read=False) == _APP_URL
    assert run_git(workspace, ["ls-remote", "--get-url", _APP_URL]) == _APP_URL


def test_a_workspace_with_no_gateway_reads_with_its_own_configuration_alone(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")

    assert run_git(workspace, ["ls-remote", "--get-url", _APP_URL], is_gateway_used=True) == _APP_URL


def test_landing_is_refused_over_a_merge_the_primary_checkout_has_not_concluded(tmp_path: Path) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    run_git(workspace, ["checkout", "--quiet", "-b", "theirs"])
    write_workspace_file(workspace, "README.md", "their workspace\n")
    theirs = commit_all(workspace, "their README")
    run_git(workspace, ["checkout", "--quiet", "main"])
    write_workspace_file(workspace, "README.md", "our workspace\n")
    commit_all(workspace, "our README")
    with pytest.raises(GitCommandError, match="CONFLICT"):
        run_git(workspace, ["merge", "--no-edit", theirs])
    write_workspace_file(workspace, "README.md", "the resolution, not committed yet\n")
    # What the installer would land: a commit made beside the primary branch.
    landing = run_git(workspace, ["commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "app-install: install notes"])

    with pytest.raises(GitCommandError, match="in the middle of a merge"):
        land_commit(workspace, landing)

    assert run_git(workspace, ["rev-parse", "MERGE_HEAD"]) == theirs
    assert (workspace / "README.md").read_text() == "the resolution, not committed yet\n"


def test_a_repository_that_refuses_an_anonymous_read_is_read_again_with_the_workspaces_access(
    tmp_path: Path, repository_host: RepositoryHostStandIn
) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")

    with pytest.raises(RepositoryUnreachableError, match="without the workspace's GitHub access") as raised:
        fetch_commit(workspace, repository_host.repository_url, "main")

    assert raised.value.is_access_needed is True
    assert len(repository_host.requested_paths) == 2


def test_a_read_that_fails_for_a_reason_no_credential_changes_is_not_tried_again(
    tmp_path: Path, repository_host: RepositoryHostStandIn
) -> None:
    workspace = make_workspace_repository(tmp_path / "workspace")
    repository_host.status = 500

    with pytest.raises(RepositoryUnreachableError, match="returned error: 500") as raised:
        fetch_commit(workspace, repository_host.repository_url, "main")

    assert raised.value.is_access_needed is False
    assert len(repository_host.requested_paths) == 1
