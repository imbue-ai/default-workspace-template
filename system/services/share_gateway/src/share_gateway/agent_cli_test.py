"""The ``agent-access`` CLI as the desktop runs it: ``--json`` answers on stdout, JSON refusals on stderr."""

import json
from pathlib import Path

import pytest

from share_gateway.agent_cli import run
from share_gateway.agent_grants import AGENT_INVITES_FILE
from share_gateway.agent_grants import load_agent_invites
from share_gateway.testing import make_workspace_repo

_PRIMER = "# {{workspace_name}}\n"
_INVITE = "Join {{auth_origin}} with {{code}} as {{name_placeholder}}\n"


@pytest.fixture
def workspace_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = make_workspace_repo(tmp_path, _PRIMER, _INVITE)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path / "state"))
    return repo


def test_invite_and_list_answer_json_on_stdout(workspace_cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["invite", "--granter", "user:owner", "--name-hint", "Ada", "--json"]) == 0
    invite = json.loads(capsys.readouterr().out)
    assert invite["granter"] == "user:owner" and invite["scope"] == "workspace"
    assert invite["is_published"] is False and invite["auth_origin"] == ""
    assert invite["code"] in invite["instructions"] and "as Ada" in invite["instructions"]
    assert len(load_agent_invites(AGENT_INVITES_FILE)) == 1

    assert run(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"agents": []}
    events = (workspace_cwd.parent / "state" / "events" / "agent_access" / "events.jsonl").read_text()
    minted_event = json.loads(events.splitlines()[0])
    assert minted_event["type"] == "invite_minted" and minted_event["via"].startswith("cli")


def test_a_refusal_is_one_json_object_on_stderr_with_json_and_a_line_without(
    workspace_cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["revoke", "va-" + "0" * 32, "--revoker", "user:owner", "--owner", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": "no agent va-" + "0" * 32,
        "kind": "refused",
    }

    assert run(["revoke", "va-" + "0" * 32, "--revoker", "user:owner", "--owner"]) == 2
    assert capsys.readouterr().err == f"agent-access: no agent va-{'0' * 32}\n"

    assert run(["invite", "--granter", "user:owner", "--scope", "no-such-app", "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["kind"] == "refused"

    assert run(["invite", "--granter", "user:owner", "--expires-in-days", "0", "--json"]) == 2
    assert "positive number of days" in json.loads(capsys.readouterr().err)["error"]
    assert len(load_agent_invites(AGENT_INVITES_FILE)) == 0


def test_a_malformed_grants_file_is_reported_as_misconfigured(
    workspace_cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    grants_path = workspace_cwd / "data" / ".secrets" / "agent_access" / "grants.toml"
    grants_path.parent.mkdir(parents=True)
    grants_path.write_text("[[agents]]\nagent_id = 'va-bad'\n")
    assert run(["list", "--json"]) == 3
    assert json.loads(capsys.readouterr().err)["kind"] == "misconfigured"


def test_changes_refuses_a_naive_since(workspace_cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["changes", "--since", "2026-10-15T18:00:00", "--json"]) == 2
    assert "timezone" in json.loads(capsys.readouterr().err)["error"]
    assert run(["changes", "--since", "2000-01-01T00:00:00Z"]) == 0
    feed = json.loads(capsys.readouterr().out)
    assert [entry["subject"] for entry in feed["commits"]] == ["Initial workspace commit"]
