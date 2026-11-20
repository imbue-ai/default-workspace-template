from pathlib import Path

import pytest

from app_install.git import run_git
from app_install.testing import make_workspace_repository

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
