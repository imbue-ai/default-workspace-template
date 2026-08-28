import json
import re
from importlib import resources

import pytest

from imbue.minds_evals import forward_instance
from imbue.minds_evals import minds_bridge
from imbue.minds_evals import ui_flows
from imbue.minds_evals.testing import FAKE_WORKSPACE_AGENT_ID


def _action(
    kind: ui_flows.FlowActionKind, role: str = "", target: str = "", text: str = "", amount: int = 0
) -> ui_flows.FlowAction:
    return ui_flows.FlowAction(kind=kind, role=role, target=target, text=text, amount=amount, reasoning="because")


# --- classifying what the executor reported ---


@pytest.mark.parametrize(
    "reason",
    [
        ui_flows.REASON_BROWSER_LAUNCH_FAILED,
        ui_flows.REASON_CDP_CONNECT_FAILED,
        ui_flows.REASON_FORWARD_UNREACHABLE,
        ui_flows.REASON_TUNNEL_DOWN,
        ui_flows.REASON_TLS_REFUSED,
        ui_flows.REASON_STEP_BRIDGE_FAILED,
    ],
)
def test_every_executor_level_reason_stops_the_flow(reason: str) -> None:
    # Each of these means the browser cannot be driven any further, so the flow ends as
    # unmeasurable rather than being scored against the app.
    assert ui_flows.is_instrument_reason(reason) is True


@pytest.mark.parametrize("reason", ["", ui_flows.REASON_ACTION_TIMED_OUT, ui_flows.REASON_STEP_BUDGET_EXHAUSTED])
def test_an_action_that_simply_did_not_work_keeps_the_flow_going(reason: str) -> None:
    # An element that is not there is the app falling short; the browser is fine and the next step
    # sees the real page, so writing the flow off here would excuse a genuine app failure.
    assert ui_flows.is_instrument_reason(reason) is False


def test_parse_step_result_reads_the_scripts_json_contract() -> None:
    outcome = ui_flows.parse_step_result(
        json.dumps(
            {
                "is_ok": True,
                "reason": "",
                "detail": "",
                "url": "https://todo-x.agent-abc.localhost:8431/",
                "title": "Todo",
                "snapshot": "- textbox 'Add a task'",
                "screenshot_path": "/logs/agent/verification/flows/persistence/step_001.png",
            }
        )
    )

    assert outcome.is_ok is True
    assert outcome.screenshot_name == "step_001.png"
    # URL and title lead the state, because ruling on a flow can turn on them -- a reload that lost
    # the session lands somewhere else entirely.
    assert outcome.state_text.startswith("page https://todo-x.agent-abc.localhost:8431/ (Todo)")
    assert "- textbox 'Add a task'" in outcome.state_text


def test_parse_step_result_treats_non_json_as_the_bridge_failing() -> None:
    # The script prints JSON for every outcome including its own failures, so output that is not
    # JSON means it never ran -- which is nothing to do with the app.
    outcome = ui_flows.parse_step_result("uv: command not found")

    assert (outcome.is_ok, outcome.reason) == (False, ui_flows.REASON_STEP_BRIDGE_FAILED)
    assert ui_flows.is_instrument_reason(outcome.reason) is True


def test_parse_step_result_keeps_the_page_when_the_action_failed() -> None:
    # The most useful thing a failed step can record is what the page actually showed.
    outcome = ui_flows.parse_step_result(
        json.dumps(
            {
                "is_ok": False,
                "reason": ui_flows.REASON_ACTION_TIMED_OUT,
                "detail": "locator resolved to 0 elements",
                "url": "https://todo-x.agent-abc.localhost:8431/",
                "title": "Todo",
                "snapshot": "- heading 'Things to do'",
            }
        )
    )

    assert outcome.reason == ui_flows.REASON_ACTION_TIMED_OUT
    assert "Things to do" in outcome.state_text


# --- the step request ---


def test_build_step_request_installs_the_session_cookie_on_the_first_step_only() -> None:
    # It rides the request that opens the app, so the very first navigation is already
    # authenticated and never takes the proxy's login redirect.
    origin = forward_instance.forwarded_origin("todo-x", FAKE_WORKSPACE_AGENT_ID, 8431)
    domain = forward_instance.session_cookie_domain(FAKE_WORKSPACE_AGENT_ID)
    endpoint = ui_flows.cdp_endpoint(ui_flows.flow_browser_port(0))
    opening = json.loads(
        ui_flows.build_step_request(
            _action(ui_flows.FlowActionKind.OPEN, text=origin),
            "/logs/shot.png",
            cdp_endpoint_url=endpoint,
            preauth_cookie="tok",
            cookie_domain=domain,
        )
    )
    later = json.loads(
        ui_flows.build_step_request(
            _action(ui_flows.FlowActionKind.CLICK, role="button", target="Add"),
            "/logs/shot.png",
            cdp_endpoint_url=endpoint,
            preauth_cookie="",
            cookie_domain=domain,
        )
    )

    assert opening["cookie"]["name"] == forward_instance.SESSION_COOKIE_NAME
    assert opening["cookie"]["value"] == "tok"
    # Every attribute the proxy's own session cookie carries, so the browser holds the same cookie.
    assert (
        opening["cookie"]["domain"],
        opening["cookie"]["path"],
        opening["cookie"]["is_secure"],
        opening["cookie"]["is_http_only"],
        opening["cookie"]["same_site"],
    ) == (domain, "/", True, True, "None")
    # A later step lands in the same browser, which is still holding the session, and re-sends
    # nothing.
    assert later["cookie"] is None
    assert later["cdp_endpoint"] == endpoint


def test_build_step_request_carries_role_and_name_rather_than_an_index() -> None:
    request = json.loads(
        ui_flows.build_step_request(
            _action(ui_flows.FlowActionKind.INPUT, role="textbox", target="Add a task", text="buy milk"),
            "/logs/shot.png",
            cdp_endpoint_url=ui_flows.cdp_endpoint(ui_flows.flow_browser_port(0)),
            preauth_cookie="",
            cookie_domain="",
        )
    )

    assert request["action"] == {
        "kind": "input",
        "role": "textbox",
        "target": "Add a task",
        "text": "buy milk",
        "amount": 0,
    }


def test_step_command_runs_the_uploaded_script_in_the_boxs_own_venv() -> None:
    command = ui_flows.step_command('{"cdp_endpoint": "http://127.0.0.1:9333"}')

    assert ui_flows.BOX_FLOW_STEP_PATH in command
    # playwright lives in the venv the box already syncs, which is also what installed the
    # browser this script drives.
    assert command.startswith("cd /work/mngr && uv run python")


def test_browser_launch_command_asks_playwright_where_its_chromium_is() -> None:
    command = ui_flows.browser_launch_command(0)

    # The one resolution that survives a version bump: ask the installed package, in the box's own
    # venv, with the browsers root the image installed into.
    assert (
        "cd {} && PLAYWRIGHT_BROWSERS_PATH={} uv run python -c".format(
            minds_bridge.BOX_MNGR_DIR, ui_flows.BOX_PLAYWRIGHT_BROWSERS_PATH
        )
        in command
    )
    assert "chromium.executable_path" in command
    # Nothing here knows the layout UNDER that root -- the revision directory and the per-platform
    # directory below it are playwright's own business and both move between versions.
    for layout_literal in ("chrome-linux", "chrome-mac", "chrome-win", "chromium-*", "chromium_headless_shell"):
        assert layout_literal not in command


def test_browser_launch_command_keeps_the_flags_the_box_needs() -> None:
    command = ui_flows.browser_launch_command(0)

    assert "--remote-debugging-port={}".format(ui_flows.flow_browser_port(0)) in command
    # The box runs as root, where Chromium refuses to start its sandbox, and a container's default
    # /dev/shm is too small for its renderer.
    assert "--no-sandbox" in command and "--disable-dev-shm-usage" in command
    assert "setsid nohup" in command


def test_browser_launch_command_refuses_a_headless_shell_binary() -> None:
    # --headless=new needs the full Chrome build; the shell that ships beside it dies on the flag,
    # which would read as a browser that never came up rather than as the wrong binary.
    assert "*headless*)" in ui_flows.browser_launch_command(0)


def test_the_stale_browser_sweep_cannot_match_its_own_command_line() -> None:
    # `pkill -f` matches against every process's argv, including that of the shell running this
    # command. A pattern that matched itself would kill the shell before the browser ever started.
    command = ui_flows.browser_launch_command(0)

    sweep = re.search(r"pkill -f '([^']*)'", command)
    assert sweep is not None
    assert re.search(sweep.group(1), command) is None


def test_the_box_image_installs_browsers_where_the_launch_command_looks() -> None:
    # Two files, one path: the image's ENV and the constant the launch command passes. They are
    # only correct together.
    dockerfile = (resources.files("imbue.minds_evals") / "templates" / "environment" / "Dockerfile").read_text()

    assert "ENV PLAYWRIGHT_BROWSERS_PATH={}\n".format(ui_flows.BOX_PLAYWRIGHT_BROWSERS_PATH) in dockerfile


# --- deciding actions ---


def test_parse_action_reads_role_and_name() -> None:
    action = ui_flows.parse_action(
        {"action": "click", "role": "checkbox", "target": "buy milk", "reasoning": "mark it complete"}
    )

    assert action is not None
    assert (action.kind, action.role, action.target) == (ui_flows.FlowActionKind.CLICK, "checkbox", "buy milk")


def test_the_action_prompt_asks_for_the_role_and_name_the_page_shows() -> None:
    # The step script resolves an element with get_by_role(role, name=...), so a prompt that asked
    # for an index would have the model fill in something no step could ever act on.
    prompt = ui_flows._SYSTEM_PROMPT

    assert "ARIA role" in prompt and "accessible name" in prompt
    assert "index" not in prompt.lower()


def test_parse_action_rejects_an_action_that_does_not_exist() -> None:
    # Coercing an unknown verb into some other action would have the browser do a thing the agent
    # never asked for; the caller records the call as unusable instead.
    assert ui_flows.parse_action({"action": "teleport", "reasoning": "why not"}) is None


@pytest.mark.parametrize("kind", ["click", "input"])
def test_parse_action_rejects_an_element_action_that_names_no_element(kind: str) -> None:
    # An unnamed target would resolve to whatever the page happens to list first.
    assert ui_flows.parse_action({"action": kind, "role": "button", "reasoning": "clicking"}) is None


def test_reload_is_its_own_action_rather_than_a_re_open() -> None:
    # A persistence flow turns on this distinction: reloading keeps the URL and the session, while
    # navigating afresh would not be testing what the flow claims to test.
    action = ui_flows.parse_action({"action": "reload", "reasoning": "check it survived"})

    assert action is not None
    assert ui_flows.describe_action(action) == "reload the page"


def test_describe_action_names_the_element_a_reader_can_find() -> None:
    described = ui_flows.describe_action(
        _action(ui_flows.FlowActionKind.INPUT, role="textbox", target="Add a task", text="buy milk")
    )

    assert described == "type 'buy milk' into the textbox named 'Add a task'"


def test_build_action_prompt_says_so_when_nothing_has_happened_yet() -> None:
    prompt = ui_flows.build_action_prompt("Add a task.", (), "- textbox 'Add a task'")

    assert "none yet" in prompt
    assert "- textbox 'Add a task'" in prompt


def test_truncate_state_marks_a_page_it_cut() -> None:
    truncated = ui_flows.truncate_state("x" * (ui_flows.MAX_STATE_PROMPT_CHARS + 100))

    assert truncated.endswith("[...page state truncated...]")
    assert len(truncated) < ui_flows.MAX_STATE_PROMPT_CHARS + 100


def test_summarize_verifier_usage_counts_the_calls_that_produced_nothing() -> None:
    calls = (
        ui_flows.VerifierCall(tool_input={"action": "done"}, input_token_count=100, output_token_count=20),
        ui_flows.VerifierCall(tool_input=None, input_token_count=0, output_token_count=0),
    )

    usage = ui_flows.summarize_verifier_usage(calls, "claude-opus-4-8")

    assert (usage.call_count, usage.failed_call_count, usage.input_token_count) == (2, 1, 100)


def test_flow_step_record_keeps_the_page_state_verbatim() -> None:
    # The grade-time judge reads these lines instead of paying for vision on every screenshot, so
    # the state must not be abbreviated on the way in.
    state = "page https://x/ (Todo)\n" + "- button 'delete'\n" * 500

    record = json.loads(ui_flows.flow_step_record(0, "click the button", "deleting", state, "step_000.png", "", "t"))

    assert record["state"] == state
    assert (record["step_index"], record["screenshot"]) == (0, "step_000.png")


def test_flow_step_record_says_when_the_action_never_ran() -> None:
    # The judge rules on the `expect` from this log, so a step showing a click next to
    # an unchanged screenshot -- with no note that it was rejected -- would mislead it.
    record = json.loads(
        ui_flows.flow_step_record(2, "click the button named 'Delete'", "trying", "- heading", "s.png", "no such", "t")
    )

    assert record["error"] == "no such"
