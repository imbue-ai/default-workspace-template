import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Final

import pytest
from harbor.environments.base import ExecResult

from imbue.imbue_common.modal_image_requirements import IMAGE_REQUIREMENTS_FILENAME
from imbue.imbue_common.modal_image_requirements import image_pinned_app_dir
from imbue.imbue_common.modal_image_requirements import image_requirements_path
from imbue.minds_evals import minds_bridge
from imbue.minds_evals.errors import BoxCommandError
from imbue.minds_evals.errors import ModalNameBudgetError
from imbue.minds_evals.errors import WorkspaceCreateError
from imbue.minds_evals.minds_bridge import ACCOUNTS_PATH
from imbue.minds_evals.minds_bridge import ACCOUNT_FLOW_METHOD_API_KEY
from imbue.minds_evals.minds_bridge import ACCOUNT_FLOW_PATH_TEMPLATE
from imbue.minds_evals.minds_bridge import AGENTS_PATH
from imbue.minds_evals.minds_bridge import AUTH_MODE_API_KEY
from imbue.minds_evals.minds_bridge import AccountRecord
from imbue.minds_evals.minds_bridge import AccountSignIn
from imbue.minds_evals.minds_bridge import CHAT_APP_FALLBACK_URL
from imbue.minds_evals.minds_bridge import CLAUDE_AUTH_STATUS_PATH
from imbue.minds_evals.minds_bridge import MODEL_CHOICE_PATH_TEMPLATE
from imbue.minds_evals.minds_bridge import ModelSwitchOutcome
from imbue.minds_evals.minds_bridge import WORKSPACE_APPS_REGISTRY
from imbue.minds_evals.minds_bridge import WaitHeartbeat
from imbue.minds_evals.minds_bridge import WorkspaceSignIn
from imbue.minds_evals.minds_bridge import _WAIT_HEARTBEAT_SECONDS
from imbue.minds_evals.minds_bridge import authenticate_workspace
from imbue.minds_evals.minds_bridge import build_box_env
from imbue.minds_evals.minds_bridge import build_create_payload
from imbue.minds_evals.minds_bridge import build_credential_lines
from imbue.minds_evals.minds_bridge import chat_url_shell_snippet
from imbue.minds_evals.minds_bridge import create_chat_agent
from imbue.minds_evals.minds_bridge import create_workspace_and_wait
from imbue.minds_evals.minds_bridge import derive_modal_environment_name
from imbue.minds_evals.minds_bridge import describe_agents_listing
from imbue.minds_evals.minds_bridge import destroy_workspaces
from imbue.minds_evals.minds_bridge import fetch_account
from imbue.minds_evals.minds_bridge import fetch_event_total
from imbue.minds_evals.minds_bridge import fetch_events_window
from imbue.minds_evals.minds_bridge import fetch_minds_activation_env
from imbue.minds_evals.minds_bridge import load_modal_token_env
from imbue.minds_evals.minds_bridge import parse_activation_exports
from imbue.minds_evals.minds_bridge import parse_agent_ssh_info
from imbue.minds_evals.minds_bridge import parse_curl_response
from imbue.minds_evals.minds_bridge import read_box_file_tail
from imbue.minds_evals.minds_bridge import redact_secret
from imbue.minds_evals.minds_bridge import resolve_chat_agent_id
from imbue.minds_evals.minds_bridge import run_in_workspace
from imbue.minds_evals.minds_bridge import send_chat_message
from imbue.minds_evals.minds_bridge import service_log_path
from imbue.minds_evals.minds_bridge import sign_in_via_accounts_flow
from imbue.minds_evals.minds_bridge import snapshot_workspace
from imbue.minds_evals.minds_bridge import start_backend
from imbue.minds_evals.minds_bridge import start_proxy
from imbue.minds_evals.minds_bridge import start_reverse_tunnel
from imbue.minds_evals.minds_bridge import switch_model_choice
from imbue.minds_evals.minds_bridge import wait_for_auth_endpoint
from imbue.minds_evals.minds_bridge import workspace_curl
from imbue.minds_evals.minds_bridge import workspace_curl_command
from imbue.minds_evals.mock_environment_test import MockBoxEnvironment
from imbue.minds_evals.mock_environment_test import ScriptedExecRule
from imbue.minds_evals.mock_environment_test import curl_stdout
from imbue.minds_evals.mock_environment_test import failed_result
from imbue.minds_evals.mock_environment_test import mngr_exec_json
from imbue.minds_evals.mock_environment_test import ok_result
from imbue.minds_evals.template_loading import TEMPLATES_DIR


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


def test_derive_modal_environment_name_is_the_concatenation_mngr_makes() -> None:
    assert derive_modal_environment_name(_ACTIVATION_ENV, "evals-todo-app-cafe1234") == (
        "minds-staging-evals-todo-app-cafe1234"
    )


def test_derive_modal_environment_name_refuses_a_name_mngr_would_truncate() -> None:
    with pytest.raises(ModalNameBudgetError, match="truncate"):
        derive_modal_environment_name(_ACTIVATION_ENV, "z" * 60)


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


def _listed_chat(agent_id: str, name: str) -> dict:
    """One `/api/agents` entry: the workspace lists a chat under its true name, and carries no
    labels at all -- those reach clients over its WebSocket, which this bridge cannot read."""
    return {"id": agent_id, "name": name, "state": "WAITING"}


_SYSTEM_SERVICES_ENTRY: Final[dict[str, str]] = {"id": "sys-1", "name": "system-services", "state": "WAITING"}


def test_resolve_chat_agent_id_finds_the_chat_created_under_the_requested_name() -> None:
    agents = [_SYSTEM_SERVICES_ENTRY, _listed_chat("chat-9", "EVAL-todo-app-x")]

    assert resolve_chat_agent_id(agents, "EVAL-todo-app-x") == "chat-9"
    # A listing without it -- a workspace that has no such chat, or one whose create is still in
    # flight and therefore not listed yet -- names nothing.
    assert resolve_chat_agent_id([_SYSTEM_SERVICES_ENTRY], "EVAL-todo-app-x") is None
    assert resolve_chat_agent_id([], "EVAL-x") is None


def test_resolve_chat_agent_id_picks_the_requested_chat_out_of_several() -> None:
    # A workspace with chats of its own must hand back the one that was asked for, never whichever
    # happens to be listed first.
    agents = [_SYSTEM_SERVICES_ENTRY, _listed_chat("chat-1", "Chat-1"), _listed_chat("chat-2", "EVAL-todo-app-x")]

    assert resolve_chat_agent_id(agents, "EVAL-todo-app-x") == "chat-2"
    assert resolve_chat_agent_id(agents, "EVAL-unmatched") is None


def test_resolve_chat_agent_id_matches_the_true_name_a_display_name_becomes() -> None:
    # A chat is listed under the true name the workspace derives from the display name it was
    # created with: spaces become dashes, and the comparison ignores case -- which is exactly the
    # identity a colliding create is refused on.
    agents = [_listed_chat("chat-9", "Eval-Todo-App")]

    assert resolve_chat_agent_id(agents, "eval todo app") == "chat-9"
    assert resolve_chat_agent_id(agents, "eval-todo-app!") == "chat-9"


def test_parse_curl_response_separates_the_status_from_the_body() -> None:
    response = parse_curl_response('{"agent_id": "chat-1"}\n201')

    assert (response.status, response.body) == (201, {"agent_id": "chat-1"})
    # A body-less answer still carries its status, and a capture with no status line at all is the
    # call never having reached the endpoint.
    body_less = parse_curl_response("\n204")
    assert (body_less.status, body_less.body) == (204, None)
    assert parse_curl_response("").status == 0
    assert parse_curl_response("curl: (7) Failed to connect").status == 0
    # A non-JSON error page is the endpoint answering, so its status must survive -- and so must the
    # text, since it is the only account of the failure a trial log would otherwise get.
    unparseable = parse_curl_response("<html>nope</html>\n502")
    assert (unparseable.status, unparseable.body, unparseable.text) == (502, None, "<html>nope</html>")


def test_workspace_curl_targets_the_chat_app_at_the_url_the_registry_holds() -> None:
    # Every route the bridge calls is the chat app's, which registers its own port in the
    # workspace's registry at startup; the call reads that row in the same bridged exec as the curl,
    # and falls back to the template's default port for a workspace whose registry has no row yet.
    command = workspace_curl_command(AGENTS_PATH, None)

    assert WORKSPACE_APPS_REGISTRY in command
    assert "'chat'" in command
    assert "chat_url={}".format(CHAT_APP_FALLBACK_URL) in command
    assert command.endswith('"$chat_url"{}'.format(AGENTS_PATH))
    assert "-X POST" not in command

    posted = workspace_curl_command("/api/agents/create-chat", '{"name": "x"}')
    assert "-X POST" in posted and '-d \'{"name": "x"}\'' in posted


def _resolved_chat_url(repo_root: Path) -> str:
    """What the snippet leaves in ``$chat_url`` when run from the workspace repo root, as the bridged
    exec runs it (``mngr exec`` runs its command in the workspace agent's work_dir)."""
    result = subprocess.run(
        ["bash", "-c", "{}; printf '%s' \"$chat_url\"".format(chat_url_shell_snippet())],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_chat_url_shell_snippet_reads_the_registry_row_and_otherwise_falls_back(tmp_path: Path) -> None:
    # The snippet silences every failure and falls back, so only running it shows that the embedded
    # program survives its quoting and actually reads the row: the fallback must be the answer for a
    # missing registry and a registry without the row, and nothing else.
    assert _resolved_chat_url(tmp_path) == CHAT_APP_FALLBACK_URL

    registry = tmp_path / WORKSPACE_APPS_REGISTRY
    registry.parent.mkdir(parents=True)
    registry.write_text('[[apps]]\nname = "system_interface"\nurl = "http://localhost:8000"\n')
    assert _resolved_chat_url(tmp_path) == CHAT_APP_FALLBACK_URL

    registry.write_text(
        '[[apps]]\nname = "system_interface"\nurl = "http://localhost:8000"\n\n'
        '[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:8017"\n'
    )
    assert _resolved_chat_url(tmp_path) == "http://127.0.0.1:8017"


def test_workspace_curl_keeps_only_a_failure_that_said_something(tmp_path: Path) -> None:
    # A curl that could not reach the endpoint exits non-zero having printed nothing but its own
    # 000, which is no account of anything; a bridge that could not run the command at all is the
    # failure that leaves a detail worth reporting. Callers carry that detail across retries, so
    # the first must not read as having spoken.
    curl_never_connected = ok_result(
        json.dumps({"results": [{"agent": "ws-1", "stdout": "\n000", "stderr": "", "success": False}]})
    )
    environment = MockBoxEnvironment(tmp_path, [ScriptedExecRule(AGENTS_PATH, [curl_never_connected])])

    response = asyncio.run(workspace_curl(environment, {}, "ws-1", AGENTS_PATH, None))

    assert (response.status, response.body, response.text) == (0, None, "")

    bridge_failed = MockBoxEnvironment(
        tmp_path, [ScriptedExecRule(AGENTS_PATH, [failed_result("mngr exec: agent not reachable")])]
    )

    response = asyncio.run(workspace_curl(bridge_failed, {}, "ws-1", AGENTS_PATH, None))

    assert (response.status, response.text) == (0, "mngr exec: agent not reachable")


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


def _create_chat_rule(*results: ExecResult) -> ScriptedExecRule:
    return ScriptedExecRule("/api/agents/create-chat", list(results))


# The budget a create-chat call gets in these tests. Bounded rather than effectively infinite: the
# tests below assert that the retry loop *stops*, and a scripted rule repeats its last answer
# forever, so a regression that kept retrying would otherwise hang the suite instead of failing it.
# Against an in-memory environment answering synchronously this is orders of magnitude more than the
# one retry any of them needs.
_CREATE_CHAT_BUDGET_SECONDS: Final[float] = 5.0
# The name the driver asks for (it names the chat after the workspace host) and the account the
# sign-in minted for it to bind to.
_CHAT_DISPLAY_NAME: Final[str] = "EVAL-todo-app"
_CHAT_ACCOUNT_ID: Final[str] = "acct-1"


def _run_create_chat(
    environment: MockBoxEnvironment,
    account_id: str = _CHAT_ACCOUNT_ID,
    budget_seconds: float = _CREATE_CHAT_BUDGET_SECONDS,
) -> str | None:
    return asyncio.run(
        create_chat_agent(
            environment,
            {},
            "ws-1",
            _CHAT_DISPLAY_NAME,
            account_id,
            deadline=time.time() + budget_seconds,
            poll_seconds=0.01,
        )
    )


def _create_chat_call_count(environment: MockBoxEnvironment) -> int:
    return len([command for command in environment.exec_commands if "create-chat" in command])


def test_create_chat_agent_returns_the_created_agent_id(tmp_path: Path) -> None:
    created = json.dumps({"agent_id": "chat-1", "name": "eval-todo-app", "display_name": "EVAL-todo-app"})
    environment = MockBoxEnvironment(tmp_path, [_create_chat_rule(ok_result(curl_stdout(created, status=201)))])

    assert _run_create_chat(environment) == "chat-1"

    # The chat is created under the requested name and bound to the account the sign-in minted; a
    # chat bound to no account can never take a turn.
    create_command = next(command for command in environment.exec_commands if "create-chat" in command)
    assert '"name": "{}"'.format(_CHAT_DISPLAY_NAME) in create_command
    assert '"account_id": "{}"'.format(_CHAT_ACCOUNT_ID) in create_command


def test_create_chat_agent_leaves_out_an_account_it_was_not_given(tmp_path: Path) -> None:
    # No account id means the workspace picks the one it used most recently, which it does for an
    # absent field exactly as for an empty one.
    created = json.dumps({"agent_id": "chat-1"})
    environment = MockBoxEnvironment(tmp_path, [_create_chat_rule(ok_result(curl_stdout(created, status=201)))])

    assert _run_create_chat(environment, account_id="") == "chat-1"
    create_command = next(command for command in environment.exec_commands if "create-chat" in command)
    assert "account_id" not in create_command


def test_create_chat_agent_retries_only_while_the_endpoint_is_not_answering(tmp_path: Path) -> None:
    # A chat app that is still coming up answers nothing at all; that is the one case worth
    # waiting out, since the workspace is still on its way up.
    created = json.dumps({"agent_id": "chat-1"})
    environment = MockBoxEnvironment(
        tmp_path,
        [_create_chat_rule(failed_result("mngr exec: not reachable"), ok_result(curl_stdout(created, status=201)))],
    )

    assert _run_create_chat(environment) == "chat-1"
    assert _create_chat_call_count(environment) == 2


def test_create_chat_agent_gives_up_when_the_endpoint_never_answers(tmp_path: Path) -> None:
    # A workspace whose chat app never comes up answers nothing, however long it is asked.
    # The retry has to end at the deadline rather than spin, and the trial's only account of why is
    # what the attempts left behind -- so a later attempt that says nothing must not erase the one
    # that did.
    environment = MockBoxEnvironment(
        tmp_path,
        [
            _create_chat_rule(
                failed_result("mngr exec: agent ws-1 is not reachable"),
                ok_result(mngr_exec_json("\n000")),
            )
        ],
    )

    assert _run_create_chat(environment, budget_seconds=0.2) is None
    assert _create_chat_call_count(environment) > 1


def test_create_chat_agent_gives_up_on_a_refusal(tmp_path: Path) -> None:
    # A refusal is the workspace's own answer, not a workspace that is still booting: retrying it
    # would burn the case budget on a request that can never succeed.
    refusal = json.dumps({"detail": "no provider account is configured"})
    environment = MockBoxEnvironment(tmp_path, [_create_chat_rule(ok_result(curl_stdout(refusal, status=400)))])

    assert _run_create_chat(environment) is None
    assert _create_chat_call_count(environment) == 1


def test_create_chat_agent_gives_up_on_an_answer_that_is_not_json(tmp_path: Path) -> None:
    # The endpoint's own refusals are all JSON, so an answer that is not is something else
    # answering -- an unhandled traceback page, or a proxy in front of the chat app. It is
    # still the call being answered, so it is final, and the page is what a trial log reports.
    environment = MockBoxEnvironment(
        tmp_path, [_create_chat_rule(ok_result(curl_stdout("<html>Internal Server Error</html>", status=500)))]
    )

    assert _run_create_chat(environment) is None
    assert _create_chat_call_count(environment) == 1


def test_create_chat_agent_resolves_the_existing_chat_when_the_name_is_taken(tmp_path: Path) -> None:
    # The name is held by a chat the driver itself asked for: a create whose answer was lost still
    # made one, and an in-flight create counts as taken too. That chat is the one to drive, so the
    # collision is answered from the agents listing rather than failing a perfectly usable workspace.
    conflict = json.dumps({"detail": "an agent named EVAL-todo-app already exists"})
    listing = json.dumps({"agents": [_SYSTEM_SERVICES_ENTRY, _listed_chat("chat-7", "EVAL-todo-app")]})
    environment = MockBoxEnvironment(
        tmp_path,
        [
            _create_chat_rule(ok_result(curl_stdout(conflict, status=409))),
            ScriptedExecRule("/api/agents", [ok_result(curl_stdout(listing))]),
        ],
    )

    assert _run_create_chat(environment) == "chat-7"


def test_create_chat_agent_gives_up_when_the_taken_name_is_held_by_nothing(tmp_path: Path) -> None:
    # A workspace that refuses the name as taken and then never lists anything holding it leaves
    # the driver no chat to drive, so the resolution has to end at the deadline rather than spin.
    conflict = json.dumps({"detail": "an agent named EVAL-todo-app already exists"})
    listing = json.dumps({"agents": [_SYSTEM_SERVICES_ENTRY]})
    environment = MockBoxEnvironment(
        tmp_path,
        [
            _create_chat_rule(ok_result(curl_stdout(conflict, status=409))),
            ScriptedExecRule("/api/agents", [ok_result(curl_stdout(listing))]),
        ],
    )

    assert _run_create_chat(environment, budget_seconds=0.2) is None


def test_create_chat_agent_fails_when_the_workspace_names_no_agent(tmp_path: Path) -> None:
    # A created chat with no id is nothing the driver can drive, and reading it as success would
    # send every turn at an empty agent path.
    environment = MockBoxEnvironment(
        tmp_path, [_create_chat_rule(ok_result(curl_stdout(json.dumps({"name": "eval"}), status=201)))]
    )

    assert _run_create_chat(environment) is None


def test_wait_for_auth_endpoint_holds_out_for_an_endpoint_that_can_report_the_state(tmp_path: Path) -> None:
    # Being signed out is a 200 here, so the endpoint's error shapes -- JSON like everything else --
    # all mean it cannot report the state at all. Reading one as ready would post the credentials at
    # a harness that just said so, and move the failure somewhere less legible.
    cannot_report = ok_result(curl_stdout(json.dumps({"detail": "claude auth status emitted no JSON"}), status=500))
    signed_out = ok_result(curl_stdout(json.dumps({"logged_in": False, "auth_mode": "none"})))
    unreportable_only = MockBoxEnvironment(tmp_path, [ScriptedExecRule(CLAUDE_AUTH_STATUS_PATH, [cannot_report])])

    assert (
        asyncio.run(wait_for_auth_endpoint(unreportable_only, {}, "ws-1", time.time() + 0.2, poll_seconds=0.01))
        is False
    )

    recovers = MockBoxEnvironment(tmp_path, [ScriptedExecRule(CLAUDE_AUTH_STATUS_PATH, [cannot_report, signed_out])])

    assert asyncio.run(wait_for_auth_endpoint(recovers, {}, "ws-1", time.time() + 5.0, poll_seconds=0.01)) is True


# The key the workspace is signed in with below, and so the one an answer must never be logged
# still carrying.
_SIGN_IN_API_KEY: Final[str] = "sk-ant-notarealkey"


def _run_authenticate(tmp_path: Path, answer: str, status: int = 200, base_url: str = "") -> WorkspaceSignIn:
    """Sign in against a workspace whose credential endpoint answers ``answer`` with ``status``."""
    environment = MockBoxEnvironment(
        tmp_path, [ScriptedExecRule("submit-credentials", [ok_result(curl_stdout(answer, status=status))])]
    )
    return asyncio.run(authenticate_workspace(environment, {}, "ws-1", _SIGN_IN_API_KEY, base_url))


def test_authenticate_workspace_returns_the_account_a_chat_binds_to(tmp_path: Path) -> None:
    signed_in = json.dumps(
        {"account_id": "acct-9", "display": "eval", "logged_in": True, "auth_mode": AUTH_MODE_API_KEY}
    )

    sign_in = _run_authenticate(tmp_path, signed_in)

    assert (sign_in.is_signed_in, sign_in.account_id) == (True, "acct-9")


def test_authenticate_workspace_reports_a_signed_in_workspace_that_names_no_account(tmp_path: Path) -> None:
    # Without an account id the chat is created with the choice left to the workspace, which takes
    # its most recently used account -- the one just minted.
    signed_in = json.dumps({"logged_in": True, "auth_mode": AUTH_MODE_API_KEY})

    sign_in = _run_authenticate(tmp_path, signed_in)

    assert (sign_in.is_signed_in, sign_in.account_id) == (True, "")


def test_authenticate_workspace_reports_a_mode_other_than_the_one_asked_for(tmp_path: Path) -> None:
    # The endpoint runs no credential probe, so a bad key is accepted and shows up only as the mode
    # the workspace ended in. Behind a proxy the expected mode is "imbue", never a bare api_key.
    answered = json.dumps({"account_id": "acct-9", "logged_in": True, "auth_mode": AUTH_MODE_API_KEY})

    sign_in = _run_authenticate(tmp_path, answered, base_url="http://127.0.0.1:4000")

    assert (sign_in.is_signed_in, sign_in.account_id) == (False, "")


def test_authenticate_workspace_reports_an_answer_that_is_not_json(tmp_path: Path) -> None:
    # The endpoint's own refusals are JSON, so a page instead of a body is something else answering
    # -- and the workspace is no more signed in for it.
    sign_in = _run_authenticate(tmp_path, "<html>Bad Gateway</html>", status=502)

    assert (sign_in.is_signed_in, sign_in.account_id) == (False, "")


def test_authenticate_workspace_reports_a_rejected_paste(tmp_path: Path) -> None:
    rejected = json.dumps({"detail": "could not read any credentials from that paste"})

    sign_in = _run_authenticate(tmp_path, rejected, status=400)

    assert (sign_in.is_signed_in, sign_in.account_id) == (False, "")


def test_authenticate_workspace_never_logs_the_credential_it_pasted(
    tmp_path: Path, captured_log_messages: list[str]
) -> None:
    # What refuses a sign-in can quote the request that carried the paste: the endpoint reports a
    # body it could not read by rendering the validation error, and that error carries the input.
    # A trial log outlives the run and is shared, so the key must not survive into one.
    echoed = json.dumps(
        {"detail": "Invalid request body: input_value='ANTHROPIC_API_KEY={}'".format(_SIGN_IN_API_KEY)}
    )

    sign_in = _run_authenticate(tmp_path, echoed, status=400)

    assert (sign_in.is_signed_in, sign_in.account_id) == (False, "")
    assert captured_log_messages and not any(_SIGN_IN_API_KEY in message for message in captured_log_messages)
    assert any("<redacted>" in message for message in captured_log_messages)
    # The status goes in beside the detail: it is what separates a paste the endpoint could not
    # read (400) from an account it could not write (500), which are not the same failure.
    assert any("HTTP 400" in message for message in captured_log_messages)
    # And a caller with no secret to hide gets its text back, rather than the marker spliced
    # between every character.
    assert redact_secret("nothing to hide", "") == "nothing to hide"


def test_authenticate_workspace_reports_a_refusal_that_reads_like_an_auth_status(tmp_path: Path) -> None:
    # A refusal need not name a detail, and its body can still carry the very fields a successful
    # sign-in answers with. Only the status separates the two, so a workspace read on the body alone
    # would run the whole trial against an unauthenticated agent.
    refused = json.dumps({"logged_in": True, "auth_mode": AUTH_MODE_API_KEY})

    sign_in = _run_authenticate(tmp_path, refused, status=500)

    assert (sign_in.is_signed_in, sign_in.account_id) == (False, "")


# The lane the accounts flow signs a workspace in on below, and the provider the key belongs to --
# which only an api-key lane carries, since every other lane serves one provider.
_LANE_ID: Final[str] = "api-key"
_KEY_PROVIDER: Final[str] = "anthropic"
_FLOW_ID: Final[str] = "flow-9"
_FLOW_PATH: Final[str] = ACCOUNT_FLOW_PATH_TEMPLATE.format(flow_id=_FLOW_ID)
# The budget a sign-in gets in these tests. Bounded rather than effectively infinite: a scripted
# rule repeats its last answer forever, so a regression that never stopped polling would hang the
# suite instead of failing it.
_ACCOUNTS_BUDGET_SECONDS: Final[float] = 5.0
# What the workspace answers when a paste flow starts: the shape it wants the credential in, and the
# id the key is then submitted against.
_FLOW_STARTED: Final[str] = json.dumps({"flow_id": _FLOW_ID, "shape": "paste", "url": None, "code": None})


def _accounts_environment(
    tmp_path: Path, *flow_answers: ExecResult, start_answer: ExecResult | None = None
) -> MockBoxEnvironment:
    """A workspace whose accounts endpoint starts a flow and then answers ``flow_answers`` on it.

    The flow rule comes first because the accounts path is a prefix of the flow path, and rules are
    matched in order.
    """
    started = start_answer if start_answer is not None else ok_result(curl_stdout(_FLOW_STARTED))
    return MockBoxEnvironment(
        tmp_path,
        [
            ScriptedExecRule(_FLOW_PATH, list(flow_answers) or [ok_result()]),
            ScriptedExecRule(ACCOUNTS_PATH, [started]),
        ],
    )


def _run_accounts_sign_in(
    environment: MockBoxEnvironment,
    key_provider: str = _KEY_PROVIDER,
    budget_seconds: float = _ACCOUNTS_BUDGET_SECONDS,
) -> AccountSignIn:
    return asyncio.run(
        sign_in_via_accounts_flow(
            environment,
            {},
            "ws-1",
            _LANE_ID,
            _SIGN_IN_API_KEY,
            key_provider,
            deadline=time.time() + budget_seconds,
            poll_seconds=0.01,
        )
    )


def _flow_commands(environment: MockBoxEnvironment) -> list[str]:
    return [command for command in environment.exec_commands if _FLOW_PATH in command]


def test_sign_in_via_accounts_flow_returns_the_account_a_chat_binds_to(tmp_path: Path) -> None:
    settled = ok_result(curl_stdout(json.dumps({"state": "ok", "detail": None, "account_id": "acct-pi-1"})))
    environment = _accounts_environment(tmp_path, settled)

    sign_in = _run_accounts_sign_in(environment)

    assert (sign_in.is_signed_in, sign_in.account_id, sign_in.failure) == (True, "acct-pi-1", "")
    # The lane is what the flow is started on, since it decides the harness the chat will run; the
    # key is then submitted against the flow id that start answered, with the provider it belongs to.
    start_command = next(
        command for command in environment.exec_commands if ACCOUNTS_PATH in command and _FLOW_PATH not in command
    )
    assert '"lane_id": "{}"'.format(_LANE_ID) in start_command
    assert '"method_id": "{}"'.format(ACCOUNT_FLOW_METHOD_API_KEY) in start_command
    assert '"key_provider": "{}"'.format(_KEY_PROVIDER) in _flow_commands(environment)[0]


def test_sign_in_via_accounts_flow_leaves_out_a_provider_the_lane_does_not_take(tmp_path: Path) -> None:
    # A lane that serves one provider has no use for the field, and refuses a request that carries it.
    settled = ok_result(curl_stdout(json.dumps({"state": "ok", "account_id": "acct-pi-1"})))
    environment = _accounts_environment(tmp_path, settled)

    assert _run_accounts_sign_in(environment, key_provider="").is_signed_in

    assert "key_provider" not in _flow_commands(environment)[0]


def test_sign_in_via_accounts_flow_reports_a_flow_the_workspace_would_not_start(tmp_path: Path) -> None:
    # A lane the workspace does not offer is refused before any key is sent, and the trial's only
    # account of why is what the endpoint said.
    refused = ok_result(curl_stdout(json.dumps({"detail": "unknown lane"}), status=400))
    environment = _accounts_environment(tmp_path, start_answer=refused)

    sign_in = _run_accounts_sign_in(environment)

    assert not sign_in.is_signed_in
    assert sign_in.failure == "the workspace refused to start a sign-in on lane api-key: unknown lane"
    # Not a wait that ran out: the workspace answered, so the reason stands on its own rather than
    # being wrapped in a readiness reason.
    assert not sign_in.is_failure_from_waiting
    assert _flow_commands(environment) == []


def test_sign_in_via_accounts_flow_reports_a_start_that_named_no_flow(tmp_path: Path) -> None:
    # Without a flow id there is nowhere to submit the key, so a 2xx that names none is as much a
    # refusal as an outright one.
    started_without_id = ok_result(curl_stdout(json.dumps({"shape": "paste"})))
    environment = _accounts_environment(tmp_path, start_answer=started_without_id)

    sign_in = _run_accounts_sign_in(environment)

    assert not sign_in.is_signed_in
    assert "refused to start a sign-in on lane api-key" in sign_in.failure
    assert _flow_commands(environment) == []


def test_sign_in_via_accounts_flow_reports_a_key_the_harness_could_not_use(tmp_path: Path) -> None:
    # The workspace probes the provider with the key before it answers, so a rejected key is caught
    # here rather than a turn later, as an agent that says it is not logged in.
    rejected = ok_result(curl_stdout(json.dumps({"state": "failed", "detail": "the anthropic key was not accepted"})))
    environment = _accounts_environment(tmp_path, rejected)

    sign_in = _run_accounts_sign_in(environment)

    assert (sign_in.is_signed_in, sign_in.account_id) == (False, "")
    assert sign_in.failure == "the workspace rejected the key for lane api-key: the anthropic key was not accepted"
    assert not sign_in.is_failure_from_waiting


def test_sign_in_via_accounts_flow_reports_a_submit_the_workspace_refused(tmp_path: Path) -> None:
    # A submit that is refused outright never becomes a flow to poll, so it is read as the rejection
    # it is rather than waited out to the deadline.
    refused = ok_result(curl_stdout(json.dumps({"detail": "the flow has already been used"}), status=409))
    environment = _accounts_environment(tmp_path, refused)

    sign_in = _run_accounts_sign_in(environment, budget_seconds=0.2)

    assert not sign_in.is_signed_in
    assert "rejected the key for lane api-key" in sign_in.failure
    assert "the flow has already been used" in sign_in.failure
    assert len(_flow_commands(environment)) == 1


def test_sign_in_via_accounts_flow_polls_a_flow_that_has_not_settled(tmp_path: Path) -> None:
    # An api-key lane settles on the submit, but the flow may also answer that it is still working;
    # it is then polled on the same URL, with no body, until it settles.
    pending = ok_result(curl_stdout(json.dumps({"state": "pending", "detail": "probing the provider"})))
    settled = ok_result(curl_stdout(json.dumps({"state": "ok", "account_id": "acct-pi-1"})))
    environment = _accounts_environment(tmp_path, pending, pending, settled)

    sign_in = _run_accounts_sign_in(environment)

    assert (sign_in.is_signed_in, sign_in.account_id) == (True, "acct-pi-1")
    flow_commands = _flow_commands(environment)
    assert len(flow_commands) == 3
    assert "-X POST" in flow_commands[0]
    assert not any("-X POST" in command for command in flow_commands[1:])


def test_sign_in_via_accounts_flow_gives_up_on_a_flow_that_never_settles(tmp_path: Path) -> None:
    # A flow that keeps answering that it is working has to end at the deadline rather than spin,
    # and it is a wait running out -- which the caller reports as a readiness reason.
    pending = ok_result(curl_stdout(json.dumps({"state": "pending", "detail": None})))
    environment = _accounts_environment(tmp_path, pending)

    sign_in = _run_accounts_sign_in(environment, budget_seconds=0.2)

    assert (sign_in.is_signed_in, sign_in.account_id) == (False, "")
    assert sign_in.failure == "the sign-in on lane api-key never settled"
    assert sign_in.is_failure_from_waiting
    assert len(_flow_commands(environment)) > 1


def test_sign_in_via_accounts_flow_reports_a_sign_in_that_named_no_account(
    tmp_path: Path, captured_log_messages: list[str]
) -> None:
    # Without an account id the chat is created with the choice left to the workspace, which takes
    # its most recently used account -- the one just minted.
    settled = ok_result(curl_stdout(json.dumps({"state": "ok", "detail": None})))

    sign_in = _run_accounts_sign_in(_accounts_environment(tmp_path, settled))

    assert (sign_in.is_signed_in, sign_in.account_id, sign_in.failure) == (True, "", "")
    assert any("named no account" in message and _LANE_ID in message for message in captured_log_messages)


def test_sign_in_via_accounts_flow_never_logs_the_key_it_submitted(
    tmp_path: Path, captured_log_messages: list[str]
) -> None:
    # What refuses a sign-in can quote the request that carried the key, and a trial log outlives
    # the run and is shared -- so neither the log nor the reason the trial reports may carry it.
    echoed = ok_result(
        curl_stdout(json.dumps({"state": "failed", "detail": "provider refused key {}".format(_SIGN_IN_API_KEY)}))
    )
    sign_in = _run_accounts_sign_in(_accounts_environment(tmp_path, echoed))

    assert not sign_in.is_signed_in
    assert _SIGN_IN_API_KEY not in sign_in.failure
    assert "<redacted>" in sign_in.failure
    assert captured_log_messages and not any(_SIGN_IN_API_KEY in message for message in captured_log_messages)


# The accounts listing the workspace answers, in the shape the product's account screen reads: the
# harness a chat on each account runs is the workspace's own to report.
_ACCOUNTS_LISTING: Final[str] = json.dumps(
    {
        "accounts": [
            {"id": "acct-claude", "lane": "anthropic", "harness": "claude", "provider": "anthropic", "seq": 1},
            {"id": "acct-pi", "lane": "api-key", "harness": "pi-coding", "provider": "anthropic", "seq": 2},
        ],
        "mru": "acct-pi",
        "default": "acct-claude",
    }
)


def test_fetch_account_reads_the_harness_the_workspace_minted_the_account_on(tmp_path: Path) -> None:
    # The harness is what the accounts listing says it is: the lane a run asked for is a request,
    # and this is the only readback of what it actually got.
    environment = MockBoxEnvironment(
        tmp_path, [ScriptedExecRule(ACCOUNTS_PATH, [ok_result(curl_stdout(_ACCOUNTS_LISTING))])]
    )

    account = asyncio.run(fetch_account(environment, {}, "ws-1", "acct-pi"))

    assert account == AccountRecord(id="acct-pi", lane="api-key", harness="pi-coding")


def test_fetch_account_reports_an_account_the_workspace_does_not_list(
    tmp_path: Path, captured_log_messages: list[str]
) -> None:
    environment = MockBoxEnvironment(
        tmp_path, [ScriptedExecRule(ACCOUNTS_PATH, [ok_result(curl_stdout(_ACCOUNTS_LISTING))])]
    )

    assert asyncio.run(fetch_account(environment, {}, "ws-1", "acct-gone")) is None
    assert any("acct-gone" in message for message in captured_log_messages)


def test_fetch_account_reports_a_listing_it_could_not_read(tmp_path: Path, captured_log_messages: list[str]) -> None:
    # A listing that cannot be read costs the trial its harness readback and nothing else, so it is
    # reported rather than raised.
    environment = MockBoxEnvironment(
        tmp_path, [ScriptedExecRule(ACCOUNTS_PATH, [ok_result(curl_stdout("<html>Bad Gateway</html>", status=502))])]
    )

    assert asyncio.run(fetch_account(environment, {}, "ws-1", "acct-pi")) is None
    assert any("nothing readable" in message for message in captured_log_messages)


_MODEL_CHOICE_PATH: Final[str] = MODEL_CHOICE_PATH_TEMPLATE.format(agent_id="chat-1")


def _run_switch_model_choice(environment: MockBoxEnvironment) -> ModelSwitchOutcome:
    return asyncio.run(switch_model_choice(environment, {}, "ws-1", "chat-1", "haiku", "medium", False))


def test_switch_model_choice_sends_every_axis_the_endpoint_applies(tmp_path: Path) -> None:
    # All three axes ride on every call, so the endpoint applies all three rather than only what a
    # client's own diffing would have considered changed.
    applied = ok_result(curl_stdout(json.dumps({"status": "ok"})))
    environment = MockBoxEnvironment(tmp_path, [ScriptedExecRule(_MODEL_CHOICE_PATH, [applied])])

    outcome = _run_switch_model_choice(environment)

    assert outcome == ModelSwitchOutcome(is_applied=True, status=200)
    (command,) = [command for command in environment.exec_commands if _MODEL_CHOICE_PATH in command]
    assert '"model_id": "haiku"' in command
    assert '"effort": "medium"' in command
    assert '"fast": false' in command
    assert '"axes": ["model", "effort", "fast"]' in command


def test_switch_model_choice_quotes_a_configuration_error_verbatim(tmp_path: Path) -> None:
    # A refused catalog id or a missing effort level is a configuration error, never a workspace
    # fault: the endpoint's own words are what makes a typo legible from the trial listing.
    refused = ok_result(curl_stdout(json.dumps({"detail": "This model requires an effort level"}), status=400))
    environment = MockBoxEnvironment(tmp_path, [ScriptedExecRule(_MODEL_CHOICE_PATH, [refused])])

    outcome = _run_switch_model_choice(environment)

    assert outcome == ModelSwitchOutcome(is_applied=False, status=400, detail="This model requires an effort level")


def test_switch_model_choice_bounds_a_refusal_it_reports(tmp_path: Path) -> None:
    # The detail travels into the trial's own record, so an endpoint that answers with a page of
    # text must not take the record with it.
    refused = ok_result(curl_stdout(json.dumps({"detail": "x" * 900}), status=400))
    environment = MockBoxEnvironment(tmp_path, [ScriptedExecRule(_MODEL_CHOICE_PATH, [refused])])

    assert len(_run_switch_model_choice(environment).detail) == 300


def test_switch_model_choice_reports_a_workspace_that_could_not_apply_it(tmp_path: Path) -> None:
    # A 500 is the workspace failing rather than the choice being wrong, and a call that never
    # reached the endpoint at all reads back as status 0 -- neither of which applied anything.
    failing = MockBoxEnvironment(
        tmp_path / "failing",
        [
            ScriptedExecRule(
                _MODEL_CHOICE_PATH, [ok_result(curl_stdout(json.dumps({"detail": "harness down"}), status=500))]
            )
        ],
    )
    unreachable = MockBoxEnvironment(
        tmp_path / "unreachable", [ScriptedExecRule(_MODEL_CHOICE_PATH, [failed_result("mngr exec: not reachable")])]
    )

    assert _run_switch_model_choice(failing) == ModelSwitchOutcome(is_applied=False, status=500, detail="harness down")

    unreachable_outcome = _run_switch_model_choice(unreachable)

    assert (unreachable_outcome.is_applied, unreachable_outcome.status) == (False, 0)
    assert "not reachable" in unreachable_outcome.detail


_MESSAGE_PATH: Final[str] = "/api/agents/chat-1/message"


def _run_send_chat_message(environment: MockBoxEnvironment, budget_seconds: float) -> bool:
    return asyncio.run(
        send_chat_message(
            environment,
            {},
            "ws-1",
            "chat-1",
            "Build it",
            deadline=time.time() + budget_seconds,
            poll_seconds=0.01,
        )
    )


def test_send_chat_message_waits_out_a_workspace_that_is_not_ready_for_it(tmp_path: Path) -> None:
    # A chat can be listed as WAITING and still refuse a send: the listing is a live mngr discovery,
    # while the message endpoint answers from the workspace's own agent map, which a create fills
    # later (404) and a harness whose daemon is still starting refuses from (503). Both clear on
    # their own, so they are worth waiting out.
    not_found = ok_result(curl_stdout(json.dumps({"detail": "Agent 'chat-1' not found"}), status=404))
    not_ready = ok_result(curl_stdout(json.dumps({"detail": "not ready to receive messages yet"}), status=503))
    accepted = ok_result(curl_stdout(json.dumps({"status": "ok"})))
    environment = MockBoxEnvironment(tmp_path, [ScriptedExecRule(_MESSAGE_PATH, [not_found, not_ready, accepted])])

    assert _run_send_chat_message(environment, budget_seconds=5.0) is True
    assert len([command for command in environment.exec_commands if _MESSAGE_PATH in command]) == 3


def test_send_chat_message_reports_a_refusal_rather_than_a_phantom_send(tmp_path: Path) -> None:
    # The endpoint refuses in JSON, so a body alone proves nothing. Reading one as sent would leave
    # the turn loop waiting out its budget for a reply to a message that never arrived, and the
    # trial would blame the agent for the silence instead of naming the refusal.
    refused = ok_result(curl_stdout(json.dumps({"detail": "input is blocked", "kind": "input_blocked"}), status=500))
    environment = MockBoxEnvironment(tmp_path, [ScriptedExecRule(_MESSAGE_PATH, [refused])])

    assert _run_send_chat_message(environment, budget_seconds=0.2) is False


def _events_body(total: int, events: list[dict]) -> ExecResult:
    return ok_result(curl_stdout(json.dumps({"total": total, "events": events})))


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


def test_service_logs_are_kept_out_of_the_directory_harbor_empties_between_steps() -> None:
    """Anything a long-running box process holds open must not live under the agent logs dir: a
    multi-step run empties that directory before every step, unlinking the file while the writer
    keeps appending to the dead inode."""
    assert not service_log_path(minds_bridge.BOX_LOG_FILENAME).startswith(minds_bridge.BOX_LOGS_DIR + "/")
    assert service_log_path(minds_bridge.BOX_LOG_FILENAME) == "/logs/artifacts/minds/box.log"


def test_start_backend_writes_the_backend_log_where_it_survives_the_whole_trial(tmp_path: Path) -> None:
    environment = MockBoxEnvironment(tmp_path, [])

    asyncio.run(start_backend(environment, {}))

    (command,) = environment.exec_commands
    assert "> {} 2>&1".format(service_log_path(minds_bridge.BOX_LOG_FILENAME)) in command
    assert "mkdir -p {}".format(minds_bridge.BOX_SERVICE_LOGS_DIR) in command


def test_the_tunnel_and_proxy_log_beside_the_backend(tmp_path: Path) -> None:
    """Both are started once and outlive the step that started them, so both share the backend's
    fate if they log under the agent logs dir."""
    ssh_info = {"user": "root", "host": "1.2.3.4", "port": "22", "key_path": "/k"}
    tunnel_environment = MockBoxEnvironment(tmp_path / "tunnel", [])
    proxy_environment = MockBoxEnvironment(tmp_path / "proxy", [])

    asyncio.run(
        start_reverse_tunnel(tunnel_environment, {}, "ws-1", ssh_info, 4000, 60.0, is_probe_token_served=False)
    )
    asyncio.run(start_proxy(proxy_environment, {}, "model_list: []", "sk-up", "sk-trial", 4000))

    tunnel_command = tunnel_environment.exec_commands[-1]
    assert "> {} 2>&1".format(service_log_path(minds_bridge.TUNNEL_LOG_FILENAME)) in tunnel_command
    proxy_command = proxy_environment.exec_commands[-1]
    assert "> {} 2>&1".format(service_log_path(minds_bridge.PROXY_LOG_FILENAME)) in proxy_command


def test_the_proxy_is_served_by_its_own_litellm(tmp_path: Path) -> None:
    """The workspace venv's litellm cannot serve -- it carries no [proxy] extra -- and every
    `uv run` in the box re-syncs that venv, so a proxy borrowing it would die at startup or lose its
    dependencies mid-trial."""
    environment = MockBoxEnvironment(tmp_path, [])

    asyncio.run(start_proxy(environment, {}, "model_list: []", "sk-up", "sk-trial", 4000))

    command = environment.exec_commands[-1]
    assert minds_bridge.BOX_PROXY_LITELLM_PATH in command
    assert "uv run" not in command


# The pin set the box image builds the proxy venv from. Derived from the same layout helper
# modal_litellm's own drift test resolves its export through, so the two apps cannot disagree about
# where it lives; the Dockerfile names it repo-root-relative, reading it out of the staged clone it
# builds from.
_PROXY_PIN_PACKAGE: Final[str] = "modal-litellm"
_PROXY_IMAGE_REQUIREMENTS: Final[str] = "{}/{}".format(
    image_pinned_app_dir(_PROXY_PIN_PACKAGE), IMAGE_REQUIREMENTS_FILENAME
)


def test_the_box_image_builds_the_venv_the_proxy_is_started_from() -> None:
    """Two files, one path: the image creates the venv and fills it, the driver runs the litellm
    inside it. A venv created but left empty -- or filled through some other interpreter -- has no
    litellm to start, and the trial fails bring-up in the box, where the host sees only a log."""
    dockerfile = (TEMPLATES_DIR / "environment" / "Dockerfile").read_text()

    (venv_line,) = [line for line in dockerfile.splitlines() if "uv venv" in line]
    (install_line,) = [line for line in dockerfile.splitlines() if "uv pip install" in line]

    assert minds_bridge.BOX_PROXY_VENV_DIR in venv_line
    assert "--python {}".format(minds_bridge.BOX_PROXY_VENV_DIR) in install_line
    assert "--require-hashes" in install_line
    assert _PROXY_IMAGE_REQUIREMENTS in install_line


def test_the_proxy_pin_set_the_box_image_installs_is_committed() -> None:
    """The image reads that export by a path spelled out across app boundaries, and modal_litellm
    keeps it current knowing nothing of this consumer. Unchecked here, a move of the export shows up
    as a failed image build, minutes into a run on Modal."""
    repo_root = Path(__file__).resolve().parents[4]

    assert image_requirements_path(repo_root, _PROXY_PIN_PACKAGE).is_file()


def test_snapshots_stay_under_the_agent_logs_dir(tmp_path: Path) -> None:
    """A finished tarball has no writer holding it open, and the agent logs dir is downloaded once
    per step -- under the never-emptied service logs dir every earlier step's tarballs would be
    re-transferred and re-archived on every later step."""
    environment = MockBoxEnvironment(
        tmp_path,
        [
            ScriptedExecRule("tar czf /tmp/post_message_1", [ok_result(mngr_exec_json(""))]),
            ScriptedExecRule("mngr rsync", [ok_result()]),
        ],
    )

    assert asyncio.run(snapshot_workspace(environment, {}, "ws-1", "post_message_1"))

    pull_command = environment.exec_commands[-1]
    assert "{}/snapshots/".format(minds_bridge.BOX_LOGS_DIR) in pull_command


def test_a_snapshot_carries_the_transcripts_of_agents_the_run_destroyed(tmp_path: Path) -> None:
    """A worker the lead destroys after merging leaves its conversation only in mngr's preserved dir,
    which sits in the agent side's own host dir (/root/.mngr) rather than the workspace home tree."""
    environment = MockBoxEnvironment(
        tmp_path,
        [
            ScriptedExecRule("tar czf /tmp/post_message_1", [ok_result(mngr_exec_json(""))]),
            ScriptedExecRule("mngr rsync", [ok_result()]),
        ],
    )

    assert asyncio.run(snapshot_workspace(environment, {}, "ws-1", "post_message_1"))

    tar_command = environment.exec_commands[0]
    assert minds_bridge.PRESERVED_AGENT_STATE_DIR in tar_command
    assert minds_bridge.ROOT_MINDS_PROJECTS_PATTERN in tar_command
    # Named unconditionally, tar exits nonzero on a run that destroyed nothing and the whole snapshot
    # is skipped -- so the segment has to be conditional on the directory existing.
    assert "[ -d " in tar_command
    assert minds_bridge.WORKSPACE_BACKUP_ROOT in tar_command


def test_read_box_file_tail_bounds_the_read_in_the_box(tmp_path: Path) -> None:
    environment = MockBoxEnvironment(tmp_path, [ScriptedExecRule("tail -c", [ok_result("last lines\n")])])

    assert asyncio.run(read_box_file_tail(environment, {}, "/logs/artifacts/minds/box.log", 512)) == "last lines"
    assert "tail -c 512 /logs/artifacts/minds/box.log" in environment.exec_commands[0]


def test_read_box_file_tail_reads_an_absent_file_as_empty(tmp_path: Path) -> None:
    """A service that never started leaves no log, and the caller is diagnostics that must not be
    turned into a failure by one missing file. Absence is handled in the box rather than on the
    host, so the suppression has to be in the command itself."""
    environment = MockBoxEnvironment(tmp_path, [ScriptedExecRule("tail -c", [failed_result("No such file")])])

    assert asyncio.run(read_box_file_tail(environment, {}, "/nope.log", 512)) == ""
    assert "2>/dev/null || true" in environment.exec_commands[0]


def test_describe_agents_listing_names_the_three_ways_a_chat_agent_stays_unresolvable() -> None:
    """An unresolvable chat agent is nearly always one of these, and the driver log has to say
    which: the listing never answers, it is empty, or several agents make the fallback ambiguous."""
    assert "unreachable" in describe_agents_listing(None)
    assert describe_agents_listing({"agents": []}) == "an empty agents list"
    assert (
        describe_agents_listing(
            {"agents": [{"name": "system-services", "state": "WAITING"}, {"name": "other", "state": "BUSY"}]}
        )
        == "system-services(WAITING), other(BUSY)"
    )


def test_wait_heartbeat_says_it_is_still_waiting_then_holds_off(captured_log_messages: list[str]) -> None:
    """One line as soon as a poll fails, then at most one per interval: a twenty-minute wait must be
    visible in the log without becoming thousands of lines of it."""
    heartbeat = WaitHeartbeat(label="the chat agent")

    heartbeat.tick("state=unreachable")
    heartbeat.tick("state=unreachable")
    lines_within_the_interval = list(captured_log_messages)
    # Past the hold-off window, without waiting one out: the class reads a monotonic clock, so
    # moving its bookkeeping back is the same thing as time passing.
    heartbeat.last_logged_at -= _WAIT_HEARTBEAT_SECONDS + 1.0
    heartbeat.tick("state=BUSY")

    # What the log has to carry: which wait it is, how long it has run, and what the workspace was
    # answering meanwhile -- a wait that is stuck says nothing without the last of those.
    (first_line,) = lines_within_the_interval
    assert "the chat agent" in first_line
    assert "state=unreachable" in first_line
    assert len(captured_log_messages) == 2
    assert "state=BUSY" in captured_log_messages[1]
