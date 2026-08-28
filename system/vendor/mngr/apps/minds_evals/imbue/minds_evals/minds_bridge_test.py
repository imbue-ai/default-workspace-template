import asyncio
import json
from pathlib import Path

import pytest
from harbor.environments.base import ExecResult

from imbue.minds_evals.errors import BoxCommandError
from imbue.minds_evals.errors import WorkspaceCreateError
from imbue.minds_evals.minds_bridge import _resolve_chat_agent_id
from imbue.minds_evals.minds_bridge import build_box_env
from imbue.minds_evals.minds_bridge import build_create_payload
from imbue.minds_evals.minds_bridge import build_credential_lines
from imbue.minds_evals.minds_bridge import create_workspace_and_wait
from imbue.minds_evals.minds_bridge import destroy_workspaces
from imbue.minds_evals.minds_bridge import fetch_event_total
from imbue.minds_evals.minds_bridge import fetch_events_window
from imbue.minds_evals.minds_bridge import fetch_minds_activation_env
from imbue.minds_evals.minds_bridge import load_modal_token_env
from imbue.minds_evals.minds_bridge import parse_activation_exports
from imbue.minds_evals.minds_bridge import parse_agent_ssh_info
from imbue.minds_evals.minds_bridge import run_in_workspace
from imbue.minds_evals.mock_environment_test import MockBoxEnvironment
from imbue.minds_evals.mock_environment_test import ScriptedExecRule
from imbue.minds_evals.mock_environment_test import failed_result
from imbue.minds_evals.mock_environment_test import mngr_exec_json
from imbue.minds_evals.mock_environment_test import ok_result


def test_load_modal_token_env_reads_the_active_profile(tmp_path: Path) -> None:
    config = tmp_path / "modal.toml"
    config.write_text(
        '[other]\ntoken_id = "ak-other"\ntoken_secret = "as-other"\n'
        '[work]\ntoken_id = "ak-work"\ntoken_secret = "as-work"\nactive = true\n'
    )

    token_env = load_modal_token_env(config)

    assert token_env == {"MODAL_TOKEN_ID": "ak-work", "MODAL_TOKEN_SECRET": "as-work"}


def test_load_modal_token_env_raises_when_missing(tmp_path: Path) -> None:
    with pytest.raises(BoxCommandError, match="Modal auth"):
        load_modal_token_env(tmp_path / "absent.toml")


_ACTIVATION_ENV = {
    "MINDS_ROOT_NAME": "minds-staging",
    "MNGR_HOST_DIR": "/root/.minds-staging/mngr",
    "MNGR_PREFIX": "minds-staging-",
}


def test_build_box_env_scopes_the_trial_and_disables_other_providers() -> None:
    env = build_box_env(
        activation_env=_ACTIVATION_ENV,
        modal_token_env={"MODAL_TOKEN_ID": "ak", "MODAL_TOKEN_SECRET": "as"},
        user_id="trial-1-cafe1234",
        mngr_sha="c" * 40,
        minds_env="staging",
    )

    assert env["MNGR__PROVIDERS__MODAL__USER_ID"] == "trial-1-cafe1234"
    assert env["MNGR__PROVIDERS__DOCKER__IS_ENABLED"] == "false"
    assert env["MNGR_HOST_DIR"] == "/root/.minds-staging/mngr"
    # Without MNGR_PREFIX from the activation env, exec'd mngr commands resolve
    # the wrong Modal environment and silently see no workspaces.
    assert env["MNGR_PREFIX"] == "minds-staging-"
    assert env["MINDS_MODAL_EXTRA_TEMPLATE"] == "modal_eval"


def test_build_box_env_carries_no_ai_credentials() -> None:
    # Workspaces are signed in after create through the product's own endpoint. A credential in the
    # box env would be forwarded into the workspace host env file, a regime production never enters.
    env = build_box_env(
        activation_env=_ACTIVATION_ENV,
        modal_token_env={"MODAL_TOKEN_ID": "ak", "MODAL_TOKEN_SECRET": "as"},
        user_id="trial",
        mngr_sha="c" * 40,
        minds_env="staging",
    )

    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert "MINDS_EXTRA_PASS_HOST_ENV" not in env
    assert "MNGR__AGENT_TYPES__CLAUDE__ISOLATE_LOCAL_CONFIG_DIR" not in env


def test_build_credential_lines_emits_a_bare_key_without_a_base_url() -> None:
    assert build_credential_lines("sk-test", "") == "ANTHROPIC_API_KEY=sk-test\n"


def test_build_credential_lines_pairs_a_base_url_with_the_key() -> None:
    # The proxy form: a base URL is only accepted alongside a key.
    assert build_credential_lines("sk-test", "https://proxy.invalid") == (
        "ANTHROPIC_API_KEY=sk-test\nANTHROPIC_BASE_URL=https://proxy.invalid\n"
    )


def test_parse_activation_exports_reads_exports_and_ignores_unsets() -> None:
    script = (
        "# Activated env 'staging'. Source via: eval ...\n"
        "export MINDS_ROOT_NAME=minds-staging\n"
        "export MNGR_HOST_DIR=/root/.minds-staging/mngr\n"
        "export MNGR_PREFIX='minds-staging-'\n"
        "unset MODAL_PROFILE\n"
        "not an export line\n"
    )

    exports = parse_activation_exports(script)

    assert exports == {
        "MINDS_ROOT_NAME": "minds-staging",
        "MNGR_HOST_DIR": "/root/.minds-staging/mngr",
        "MNGR_PREFIX": "minds-staging-",
    }


def test_resolve_chat_agent_id_prefers_the_workspace_named_agent() -> None:
    agents = [
        {"id": "sys-1", "name": "system-services", "state": "WAITING"},
        {"id": "chat-9", "name": "eval-todo-app-x", "state": "BUSY"},
    ]

    assert _resolve_chat_agent_id(agents, "EVAL-todo-app-x") == "chat-9"


def test_resolve_chat_agent_id_falls_back_to_the_single_non_system_agent() -> None:
    agents = [
        {"id": "sys-1", "name": "system-services", "state": "WAITING"},
        {"id": "chat-9", "name": "some-other-name", "state": "WAITING"},
    ]

    assert _resolve_chat_agent_id(agents, "EVAL-unmatched") == "chat-9"


def test_resolve_chat_agent_id_returns_none_when_ambiguous() -> None:
    agents = [
        {"id": "a-1", "name": "one", "state": "WAITING"},
        {"id": "a-2", "name": "two", "state": "WAITING"},
    ]

    assert _resolve_chat_agent_id(agents, "EVAL-unmatched") is None
    assert _resolve_chat_agent_id([], "EVAL-x") is None


def test_fetch_minds_activation_env_raises_without_the_critical_exports(tmp_path: Path) -> None:
    environment = MockBoxEnvironment(
        tmp_path, [ScriptedExecRule("minds-admin env activate", [ok_result("export MINDS_ROOT_NAME=minds-staging\n")])]
    )

    with pytest.raises(BoxCommandError, match="MNGR_HOST_DIR"):
        asyncio.run(fetch_minds_activation_env(environment, "staging"))


def test_build_create_payload_matches_the_production_create_form() -> None:
    payload = build_create_payload(dwt_repo="/work/clones/todo-app", dwt_branch="", host_name="EVAL-x")

    assert payload == {
        "git_url": "/work/clones/todo-app",
        "branch": "",
        "launch_mode": "MODAL",
        "backup_provider": "CONFIGURE_LATER",
        "host_name": "EVAL-x",
    }


def test_create_workspace_and_wait_raises_on_non_202(tmp_path: Path) -> None:
    environment = MockBoxEnvironment(
        tmp_path, [ScriptedExecRule("api/v1/workspaces", [ok_result('{"error": "nope"}\n500')])]
    )

    with pytest.raises(WorkspaceCreateError, match="HTTP 500"):
        asyncio.run(
            create_workspace_and_wait(environment, {}, "8123", {"git_url": "x"}, deadline=9e12, poll_seconds=0.01)
        )


def test_create_workspace_and_wait_surfaces_operation_errors(tmp_path: Path) -> None:
    environment = MockBoxEnvironment(
        tmp_path,
        [
            ScriptedExecRule("-X POST", [ok_result('{"operation_id": "op-9"}\n202')]),
            ScriptedExecRule("operations/create/op-9", [ok_result('{"error": "provision failed"}\n200')]),
        ],
    )

    with pytest.raises(WorkspaceCreateError, match="provision failed"):
        asyncio.run(
            create_workspace_and_wait(environment, {}, "8123", {"git_url": "x"}, deadline=9e12, poll_seconds=0.01)
        )


def test_run_in_workspace_parses_the_mngr_exec_json(tmp_path: Path) -> None:
    wrapped = json.dumps({"results": [{"agent": "ws-1", "stdout": "hello\n", "stderr": "", "success": True}]})
    environment = MockBoxEnvironment(tmp_path, [ScriptedExecRule("mngr exec", [ok_result(wrapped)])])

    is_success, stdout = asyncio.run(run_in_workspace(environment, {}, "ws-1", "echo hello", 30))

    assert is_success
    assert stdout == "hello\n"
    assert any("uv run mngr exec ws-1" in command for command in environment.exec_commands)


def test_run_in_workspace_reports_failure_on_unparseable_output(tmp_path: Path) -> None:
    environment = MockBoxEnvironment(tmp_path, [ScriptedExecRule("mngr exec", [failed_result("ssh broke")])])

    is_success, detail = asyncio.run(run_in_workspace(environment, {}, "ws-1", "echo hello", 30))

    assert not is_success
    assert "ssh broke" in detail


def _events_body(total: int, events: list[dict]) -> ExecResult:
    return ok_result(mngr_exec_json(json.dumps({"total": total, "events": events})))


def test_fetch_event_total_reads_the_total(tmp_path: Path) -> None:
    environment = MockBoxEnvironment(tmp_path, [ScriptedExecRule("chat-1/events", [_events_body(7, [])])])

    total = asyncio.run(fetch_event_total(environment, {}, "ws-1", "chat-1"))

    assert total == 7


def test_fetch_events_window_returns_the_slice_and_skips_the_call_when_empty(tmp_path: Path) -> None:
    window = [{"type": "assistant_message", "text": "hi"}]
    environment = MockBoxEnvironment(tmp_path, [ScriptedExecRule("chat-1/events", [_events_body(3, window)])])

    assert asyncio.run(fetch_events_window(environment, {}, "ws-1", "chat-1", 2, 1)) == window
    # A zero-width window issues no request at all.
    before = len(environment.exec_commands)
    assert asyncio.run(fetch_events_window(environment, {}, "ws-1", "chat-1", 3, 0)) == []
    assert len(environment.exec_commands) == before


def test_destroy_workspaces_retries_once_when_agents_remain(tmp_path: Path) -> None:
    # First destroy sweep leaves an agent listed; the retry clears it.
    list_rule = ScriptedExecRule("mngr list --ids", [ok_result("agent-1\n"), ok_result("agent-1\n"), ok_result("")])
    environment = MockBoxEnvironment(tmp_path, [list_rule, ScriptedExecRule("mngr destroy", [ok_result()])])

    asyncio.run(destroy_workspaces(environment, {}))

    destroy_calls = [command for command in environment.exec_commands if "mngr destroy - --force" in command]
    assert len(destroy_calls) == 2


_SSH_LISTING = json.dumps(
    {
        "agents": [
            {
                "id": "sys-1",
                "host": {"ssh": {"user": "root", "host": "h1.modal.host", "port": 2201, "key_path": "/k1"}},
            },
            {
                "id": "ws-1",
                "host": {"ssh": {"user": "user", "host": "h2.modal.host", "port": 2202, "key_path": "/k2"}},
            },
        ]
    }
)


def test_parse_agent_ssh_info_picks_the_requested_agent() -> None:
    assert parse_agent_ssh_info(_SSH_LISTING, "ws-1") == {
        "user": "user",
        "host": "h2.modal.host",
        "port": "2202",
        "key_path": "/k2",
    }


def test_parse_agent_ssh_info_returns_none_for_an_absent_agent() -> None:
    assert parse_agent_ssh_info(_SSH_LISTING, "nope") is None


def test_parse_agent_ssh_info_returns_none_when_the_agent_has_no_ssh_endpoint() -> None:
    # A provider that exposes no SSH (or an agent listed before its host is up) must read as "no
    # tunnel possible" rather than yielding a half-built endpoint.
    listing = json.dumps({"agents": [{"id": "ws-1", "host": {}}]})

    assert parse_agent_ssh_info(listing, "ws-1") is None


def test_parse_agent_ssh_info_tolerates_a_bare_list_payload() -> None:
    listing = json.dumps([{"id": "ws-1", "host": {"ssh": {"host": "h.modal.host", "port": 22, "key_path": "/k"}}}])

    parsed = parse_agent_ssh_info(listing, "ws-1")

    assert parsed is not None
    # An absent user defaults rather than failing the lookup.
    assert parsed["user"] == "root"


def test_parse_agent_ssh_info_returns_none_on_unparseable_output() -> None:
    assert parse_agent_ssh_info("not json at all", "ws-1") is None
