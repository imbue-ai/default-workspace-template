import asyncio
import json
import time
from pathlib import Path
from typing import Final

import pytest
from harbor.environments.base import ExecResult
from loguru import logger

from imbue.minds_evals import minds_bridge
from imbue.minds_evals.errors import BoxCommandError
from imbue.minds_evals.errors import WorkspaceCreateError
from imbue.minds_evals.minds_bridge import AGENTS_PATH
from imbue.minds_evals.minds_bridge import AUTH_MODE_API_KEY
from imbue.minds_evals.minds_bridge import CLAUDE_AUTH_STATUS_PATH
from imbue.minds_evals.minds_bridge import WaitHeartbeat
from imbue.minds_evals.minds_bridge import WorkspaceSignIn
from imbue.minds_evals.minds_bridge import _WAIT_HEARTBEAT_SECONDS
from imbue.minds_evals.minds_bridge import authenticate_workspace
from imbue.minds_evals.minds_bridge import build_box_env
from imbue.minds_evals.minds_bridge import build_create_payload
from imbue.minds_evals.minds_bridge import build_credential_lines
from imbue.minds_evals.minds_bridge import create_chat_agent
from imbue.minds_evals.minds_bridge import create_workspace_and_wait
from imbue.minds_evals.minds_bridge import describe_agents_listing
from imbue.minds_evals.minds_bridge import destroy_workspaces
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
from imbue.minds_evals.minds_bridge import snapshot_workspace
from imbue.minds_evals.minds_bridge import start_backend
from imbue.minds_evals.minds_bridge import start_proxy
from imbue.minds_evals.minds_bridge import start_reverse_tunnel
from imbue.minds_evals.minds_bridge import wait_for_auth_endpoint
from imbue.minds_evals.minds_bridge import workspace_curl
from imbue.minds_evals.mock_environment_test import MockBoxEnvironment
from imbue.minds_evals.mock_environment_test import ScriptedExecRule
from imbue.minds_evals.mock_environment_test import curl_stdout
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
    # A system_interface that is still coming up answers nothing at all; that is the one case worth
    # waiting out, since the workspace is still on its way up.
    created = json.dumps({"agent_id": "chat-1"})
    environment = MockBoxEnvironment(
        tmp_path,
        [_create_chat_rule(failed_result("mngr exec: not reachable"), ok_result(curl_stdout(created, status=201)))],
    )

    assert _run_create_chat(environment) == "chat-1"
    assert _create_chat_call_count(environment) == 2


def test_create_chat_agent_gives_up_when_the_endpoint_never_answers(tmp_path: Path) -> None:
    # A workspace whose system_interface never comes up answers nothing, however long it is asked.
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
    # answering -- an unhandled traceback page, or a proxy in front of the system_interface. It is
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


def test_authenticate_workspace_never_logs_the_credential_it_pasted(tmp_path: Path) -> None:
    # What refuses a sign-in can quote the request that carried the paste: the endpoint reports a
    # body it could not read by rendering the validation error, and that error carries the input.
    # A trial log outlives the run and is shared, so the key must not survive into one.
    echoed = json.dumps(
        {"detail": "Invalid request body: input_value='ANTHROPIC_API_KEY={}'".format(_SIGN_IN_API_KEY)}
    )
    logged: list[str] = []
    handler_id = logger.add(lambda message: logged.append(message.record["message"]), level="TRACE")
    try:
        sign_in = _run_authenticate(tmp_path, echoed, status=400)
    finally:
        logger.remove(handler_id)

    assert (sign_in.is_signed_in, sign_in.account_id) == (False, "")
    assert logged and not any(_SIGN_IN_API_KEY in message for message in logged)
    assert any("<redacted>" in message for message in logged)
    # The status goes in beside the detail: it is what separates a paste the endpoint could not
    # read (400) from an account it could not write (500), which are not the same failure.
    assert any("HTTP 400" in message for message in logged)
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


def test_wait_heartbeat_says_it_is_still_waiting_then_holds_off() -> None:
    """One line as soon as a poll fails, then at most one per interval: a twenty-minute wait must be
    visible in the log without becoming thousands of lines of it."""
    heartbeat = WaitHeartbeat(label="the chat agent")
    logged: list[str] = []
    handler_id = logger.add(lambda message: logged.append(message.record["message"]), level="TRACE")
    try:
        heartbeat.tick("state=unreachable")
        heartbeat.tick("state=unreachable")
        lines_within_the_interval = list(logged)
        # Past the hold-off window, without waiting one out: the class reads a monotonic clock, so
        # moving its bookkeeping back is the same thing as time passing.
        heartbeat.last_logged_at -= _WAIT_HEARTBEAT_SECONDS + 1.0
        heartbeat.tick("state=BUSY")
    finally:
        logger.remove(handler_id)

    # What the log has to carry: which wait it is, how long it has run, and what the workspace was
    # answering meanwhile -- a wait that is stuck says nothing without the last of those.
    (first_line,) = lines_within_the_interval
    assert "the chat agent" in first_line
    assert "state=unreachable" in first_line
    assert len(logged) == 2
    assert "state=BUSY" in logged[1]
