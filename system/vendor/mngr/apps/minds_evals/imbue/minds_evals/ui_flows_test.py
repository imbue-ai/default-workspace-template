import json
import re
from importlib import resources

import pytest

from imbue.minds_evals import forward_instance
from imbue.minds_evals import minds_bridge
from imbue.minds_evals import ui_flows
from imbue.minds_evals.resources.flow_step_protocol import StepReaction
from imbue.minds_evals.testing import FAKE_WORKSPACE_AGENT_ID


def _action(
    kind: ui_flows.FlowActionKind, role: str = "", target: str = "", text: str = "", amount: int = 0, ref: str = ""
) -> ui_flows.FlowAction:
    return ui_flows.FlowAction(
        kind=kind,
        role=role,
        target=target,
        ref=ref,
        text=text,
        amount=amount,
        reasoning="the page",
        expected="something to change",
    )


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


@pytest.mark.parametrize(
    "reason",
    ["", ui_flows.REASON_ACTION_TIMED_OUT, ui_flows.REASON_STEP_BUDGET_EXHAUSTED, ui_flows.REASON_STALE_REF],
)
def test_an_action_that_simply_did_not_work_keeps_the_flow_going(reason: str) -> None:
    # An element that is not there is the app falling short, and a ref that moved is the agent
    # behind the page; the browser is fine and the next step sees the real page either way, so
    # writing the flow off here would excuse a genuine app failure.
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
        "ref": "",
        "text": "buy milk",
        "amount": 0,
    }


def test_build_step_request_carries_the_ref_of_a_nameless_element() -> None:
    request = json.loads(
        ui_flows.build_step_request(
            _action(ui_flows.FlowActionKind.CLICK, role="checkbox", ref="e9"),
            "/logs/shot.png",
            cdp_endpoint_url=ui_flows.cdp_endpoint(ui_flows.flow_browser_port(0)),
            preauth_cookie="",
            cookie_domain="",
        )
    )

    assert (request["action"]["role"], request["action"]["target"], request["action"]["ref"]) == ("checkbox", "", "e9")


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
    # An unaddressed target would resolve to whatever the page happens to list first.
    assert ui_flows.parse_action({"action": kind, "role": "button", "reasoning": "clicking"}) is None


def test_parse_action_addresses_a_nameless_element_by_its_ref() -> None:
    # The tree lists a checkbox with no name; the ref is the only handle the page gives it.
    action = ui_flows.parse_action({"action": "click", "role": "checkbox", "ref": "e9", "reasoning": "mark it"})

    assert action is not None
    assert (action.role, action.target, action.ref) == ("checkbox", "", "e9")


def test_parse_action_drops_a_ref_given_beside_a_name() -> None:
    # A named element is addressed by its name; the record must then say so, not that it was nameless.
    action = ui_flows.parse_action(
        {"action": "click", "role": "checkbox", "target": "buy milk", "ref": "e9", "reasoning": "mark it"}
    )

    assert action is not None
    assert (action.target, action.ref) == ("buy milk", "")


def test_parse_action_rejects_a_ref_with_no_role_to_check_it_against() -> None:
    # The role is what the step script checks the ref against on the page as it now stands, so a
    # ref without one would be acted on whatever it has come to name.
    assert ui_flows.parse_action({"action": "click", "ref": "e9", "reasoning": "mark it"}) is None
    assert "no role to check it against" in ui_flows.describe_unusable_action({"action": "click", "ref": "e9"})


def test_parse_action_drops_a_ref_on_a_kind_that_addresses_no_element() -> None:
    # `target_ref` on a record claims the step acted on a control the page left unnamed, so it must
    # not be set by a decision that addressed nothing at all.
    action = ui_flows.parse_action({"action": "done", "role": "checkbox", "ref": "e9", "reasoning": "finished"})

    assert action is not None
    assert action.ref == ""


def test_parse_action_rejects_a_ref_that_is_not_shaped_like_one() -> None:
    assert (
        ui_flows.parse_action({"action": "click", "role": "checkbox", "ref": "the third one", "reasoning": "x"})
        is None
    )


def test_describe_unusable_action_says_what_the_decision_asked_for() -> None:
    # The manifest entry names only the layer; this is what tells a reader of the log which of the
    # ways a decision can be unusable it was, in the decision's own words.
    assert ui_flows.describe_unusable_action(None) == "the model call produced no tool payload"
    assert "'teleport', which does not exist" in ui_flows.describe_unusable_action({"action": "teleport"})
    unaddressed = ui_flows.describe_unusable_action(
        {"action": "click", "role": "checkbox", "reasoning": "The checkbox has no name so I cannot address it."}
    )
    assert unaddressed.startswith("it asked to click a checkbox without naming it or giving its ref")
    assert "cannot address it" in unaddressed
    assert (
        ui_flows.describe_unusable_action({"action": "click", "role": "checkbox", "ref": "e9"})
        == "the payload was usable"
    )


def test_describe_unusable_action_bounds_what_it_quotes_from_the_payload() -> None:
    # The explanation is written into the manifest and into the flow log the judge reads, so a
    # payload field of any length must not be able to crowd the page state out of either.
    explained = ui_flows.describe_unusable_action({"action": "x" * 5_000})

    assert len(explained) < 300 and "'xxx" in explained


def test_the_action_prompt_allows_a_ref_only_for_a_nameless_element() -> None:
    prompt = ui_flows._SYSTEM_PROMPT

    assert "[ref=e9]" in prompt and "leave target empty" in prompt
    assert "Use a ref for nothing else" in prompt


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


def test_describe_action_says_when_the_element_had_no_name() -> None:
    # The record tells the reader what the page failed to label, not only what was clicked.
    described = ui_flows.describe_action(_action(ui_flows.FlowActionKind.CLICK, role="checkbox", ref="e9"))

    assert described == "click the checkbox that has no accessible name (ref e9)"


def test_build_action_prompt_says_so_when_nothing_has_happened_yet() -> None:
    prompt = ui_flows.build_action_prompt("Add a task.", (), "- textbox 'Add a task'")

    assert "none yet" in prompt
    assert "- textbox 'Add a task'" in prompt


def test_truncate_state_keeps_the_head_and_the_tail_of_a_page_it_cut() -> None:
    filler = "- generic [ref=e{}]: item\n" * 2000
    state = "page https://app.example/ (Roadmap)\n" + filler.format(*range(2000)) + '- complementary "Item details"'

    truncated = ui_flows.truncate_state(state)

    assert truncated.startswith("page https://app.example/ (Roadmap)")
    assert truncated.endswith('- complementary "Item details"')
    assert "[...page state truncated" in truncated
    assert len(truncated) <= ui_flows.MAX_STATE_PROMPT_CHARS + 100


def test_truncate_state_returns_a_short_page_verbatim() -> None:
    assert ui_flows.truncate_state("- button 'Add'") == "- button 'Add'"


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

    record = json.loads(
        ui_flows.flow_step_record(
            0,
            "click the button",
            "",
            "a delete button",
            "the row goes",
            "",
            StepReaction.SETTLED,
            state,
            "step_000.png",
            "",
            "t",
        )
    )

    assert record["state"] == state
    assert (record["step_index"], record["screenshot"]) == (0, "step_000.png")


def test_flow_step_record_carries_the_ref_a_nameless_element_was_addressed_by() -> None:
    # Its own field beside the prose: the judge's digest and a later measure of unlabeled controls
    # read it rather than the sentence.
    record = json.loads(
        ui_flows.flow_step_record(
            2,
            "click the checkbox that has no accessible name (ref e9)",
            "e9",
            "mark it complete",
            "the row is struck through",
            "",
            StepReaction.SETTLED,
            "- checkbox [ref=e9]",
            "s.png",
            "",
            "t",
        )
    )

    assert record["target_ref"] == "e9"


def test_flow_step_record_says_when_the_action_never_ran() -> None:
    # The judge rules on the `expect` from this log, so a step showing a click next to
    # an unchanged screenshot -- with no note that it was rejected -- would mislead it.
    record = json.loads(
        ui_flows.flow_step_record(
            2,
            "click the button named 'Delete'",
            "",
            "a delete button",
            "the row goes",
            "",
            StepReaction.UNOBSERVED,
            "- heading",
            "s.png",
            "no such",
            "t",
        )
    )

    assert record["error"] == "no such"


def test_every_record_kind_names_itself() -> None:
    # A reader dispatches on the kind rather than inferring from which fields are present, so a
    # record that named none would be read as whatever the reader's fallback happens to be.
    init = json.loads(ui_flows.flow_init_record("open it", "it opens", "https://x/", "- heading", "s.png", "t"))
    action = json.loads(
        ui_flows.flow_step_record(
            1, "click", "", "a button", "a row goes", "", StepReaction.SETTLED, "- heading", "", "", "t"
        )
    )
    final = json.loads(ui_flows.flow_final_record(2, "the row is gone", "- heading", "t"))

    assert (init["kind"], action["kind"], final["kind"]) == ("init", "action", "final")


def test_the_opening_record_carries_what_the_flow_was_asked_to_do() -> None:
    # Without it a reader meets the flow one action in, with nothing saying what it was aiming at.
    record = json.loads(
        ui_flows.flow_init_record("scroll to M-141", "M-141 is listed", "https://x/", "- heading", "step_000.png", "t")
    )

    assert (record["goal"], record["expect"]) == ("scroll to M-141", "M-141 is listed")
    assert (record["url"], record["screenshot"], record["step_index"]) == ("https://x/", "step_000.png", 0)


def test_an_action_records_what_it_predicted_and_what_followed() -> None:
    # The pair is the point: the next decision is shown both, so a wrong model of the UI reads as a
    # contradiction rather than being re-derived.
    record = json.loads(
        ui_flows.flow_step_record(
            3,
            "click the button named 'Platform'",
            "",
            "a team button",
            "the list narrows",
            "- button [pressed]",
            StepReaction.SETTLED,
            "- x",
            "",
            "",
            "t",
        )
    )

    assert (record["reasoning"], record["expected"]) == ("a team button", "the list narrows")
    assert record["observed"] == "- button [pressed]"
    # The executor's raw word travels beside the prose, so a tally of unanswered actions can count
    # it rather than parse sentences.
    assert record["reaction"] == "settled"


def test_an_unchanged_page_is_said_in_so_many_words() -> None:
    # The signal an agent loops hardest without: an action that landed and changed nothing.
    assert ui_flows.summarize_state_change("- heading\n- button", "- heading\n- button") == (
        ui_flows.UNCHANGED_STATE_SUMMARY
    )


def test_a_small_change_names_the_lines_that_moved() -> None:
    before = "- heading\n- button 'Platform' [pressed]\n- row A\n- row B"
    after = "- heading\n- button 'Platform'\n- row A"

    summary = ui_flows.summarize_state_change(before, after)

    assert "new: - button 'Platform'" in summary
    assert "gone: - button 'Platform' [pressed]" in summary
    assert "gone: - row B" in summary


def test_dropping_one_of_two_identical_rows_reads_as_a_removal() -> None:
    # The line is still on the page in its other copy, so a presence test would call this no change
    # and tell the agent its correct delete did not land.
    before = "- heading\n- listitem 'buy milk'\n- listitem 'buy milk'"
    after = "- heading\n- listitem 'buy milk'"

    summary = ui_flows.summarize_state_change(before, after)

    assert summary == "gone: - listitem 'buy milk'"


def test_the_same_lines_in_another_order_read_as_a_rearrangement() -> None:
    before = "- row A\n- row B"
    after = "- row B\n- row A"

    assert (
        ui_flows.summarize_state_change(before, after) == "the page has the same elements in a different arrangement"
    )


def test_a_change_of_blank_lines_alone_still_says_something() -> None:
    # An empty summary is how both readers spell "nothing was recorded", so a summary that was
    # produced and came out blank would be indistinguishable from one that was never written.
    summary = ui_flows.summarize_state_change("- heading\n\n- row A", "- heading\n- row A")

    assert summary == "the page has the same elements in a different arrangement"


def test_a_wholesale_change_says_only_how_much_moved() -> None:
    # Naming lines describes nothing once the page has effectively been replaced, and a hundred of
    # them in the prompt crowd out the page state the next action is chosen from.
    before = "\n".join("- row {}".format(index) for index in range(50))
    after = "\n".join("- cell {}".format(index) for index in range(50))

    summary = ui_flows.summarize_state_change(before, after)

    assert summary == "most of the page changed: 50 lines gone, 50 new, of 50"
    assert "- row 0" not in summary


def test_a_long_changed_line_is_bounded() -> None:
    before = "- heading"
    after = "- heading\n- paragraph {}".format("x" * 500)

    summary = ui_flows.summarize_state_change(before, after)

    assert len(summary.splitlines()[0]) <= ui_flows.MAX_SUMMARY_LINE_CHARS + 10
    assert summary.endswith("...")


def test_the_history_carries_the_prediction_beside_what_followed() -> None:
    # An observation only reads as confirming or contradicting something if the prediction it is
    # being compared against sits with it, in the same entry.
    entry = ui_flows.describe_step("click the button named 'X'", "the row goes", "", "gone: - row A")

    assert entry.splitlines() == [
        "click the button named 'X'",
        "   expected: the row goes",
        "   the page then: gone: - row A",
    ]


def test_a_multi_line_change_stays_under_its_own_entry() -> None:
    # The prompt numbers these entries, so a change spanning lines must not start at the margin
    # where it would read as the next numbered action.
    entry = ui_flows.describe_step("click", "two rows go", "", "gone: - row A\ngone: - row B")

    assert all(line.startswith("   ") for line in entry.splitlines()[1:])


def test_a_step_that_never_ran_says_so_in_its_entry() -> None:
    entry = ui_flows.describe_step("click the button named 'X'", "the row goes", "no such element", "")

    assert "   did not run: no such element" in entry.splitlines()


def test_an_older_entry_keeps_its_action_when_its_detail_will_not_fit() -> None:
    # What a step did stays worth knowing long after the detail of how the page moved has stopped
    # being actionable, so the detail is what gives way first.
    entries = ["action {}\n   expected: {}".format(index, "x" * 2_000) for index in range(15)]

    fitted = ui_flows.fit_history(entries)

    assert sum(len(entry) for entry in fitted) <= ui_flows.MAX_HISTORY_PROMPT_CHARS
    assert len(fitted) == len(entries)
    # The newest goes in whole; the older ones keep their action line alone.
    assert fitted[-1] == entries[-1]
    assert fitted[0] == "action 0"


def test_entries_that_do_not_fit_at_all_are_dropped_and_counted() -> None:
    # Silently losing them would let the prompt imply a step never happened for want of room.
    entries = ["action {} {}".format(index, "x" * 1_500) for index in range(15)]

    fitted = ui_flows.fit_history(entries)

    assert sum(len(entry) for entry in fitted) <= ui_flows.MAX_HISTORY_PROMPT_CHARS + 40
    assert "earlier actions not shown" in fitted[0]
    assert fitted[-1] == entries[-1]


def test_a_history_that_fits_is_left_alone() -> None:
    entries = ["click a\n   expected: b", "click c\n   expected: d"]

    assert ui_flows.fit_history(entries) == entries


def test_an_element_id_the_next_render_renumbers_is_not_a_change() -> None:
    # A rebuilt tree renumbers every ref, so comparing them makes an untouched page look rewritten:
    # on a live run one step reported 27 changed lines in a 24-line tree, nearly all of them this.
    before = '- button "Delete" [ref=e15] [cursor=pointer]\n- list [ref=e16]'
    after = '- button "Delete" [ref=e42] [cursor=pointer]\n- list [ref=e43]'

    assert ui_flows.summarize_state_change(before, after) == ui_flows.UNCHANGED_STATE_SUMMARY


def test_focus_moving_is_not_a_change() -> None:
    # A click that only focused something did nothing the flow can build on, and saying otherwise
    # reads as progress: it is what let an agent click a dead Delete button twice.
    before = '- button "Delete" [ref=e15]'
    after = '- button "Delete" [active] [ref=e15]'

    assert ui_flows.summarize_state_change(before, after) == ui_flows.UNCHANGED_STATE_SUMMARY


def test_a_real_change_survives_the_normalising() -> None:
    before = '- textbox "Add a task" [ref=e6]'
    after = '- textbox "Add a task" [active] [ref=e9]\n- text: buy milk'

    assert ui_flows.summarize_state_change(before, after) == "new: - text: buy milk"


# --- what a step's effect says, given what the executor saw the DOM do ---


@pytest.mark.parametrize(
    ("reaction", "expected_summary"),
    [
        (StepReaction.UNOBSERVED, ui_flows.UNCHANGED_STATE_SUMMARY),
        (StepReaction.NONE, ui_flows.NO_REACTION_SUMMARY),
        (StepReaction.SETTLED, ui_flows.ACKNOWLEDGED_ONLY_SUMMARY),
        (StepReaction.STILL_CHANGING, ui_flows.STILL_CHANGING_UNCHANGED_SUMMARY),
    ],
)
def test_an_unchanged_tree_is_qualified_by_what_the_dom_did(reaction: StepReaction, expected_summary: str) -> None:
    # A dead control and a control that answered with nothing but a highlight look identical in
    # the tree, and call for opposite next moves; only the executor's word on the DOM tells them
    # apart. Each sentence is distinct, so the agent cannot mistake one for another.
    state = '- button "Delete" [ref=e15]'

    assert ui_flows.summarize_step_effect(state, state, reaction) == expected_summary


def test_a_tree_that_moved_speaks_for_itself() -> None:
    before = "- heading"
    after = "- heading\n- text: buy milk"

    assert ui_flows.summarize_step_effect(before, after, StepReaction.SETTLED) == "new: - text: buy milk"
    assert ui_flows.summarize_step_effect(before, after, StepReaction.NONE) == "new: - text: buy milk"


def test_a_tree_read_while_the_page_was_still_moving_says_so() -> None:
    # The agent is about to act on this state, and it may not be the state the page ends up in.
    before = "- heading"
    after = "- heading\n- text: 250 ms"

    summary = ui_flows.summarize_step_effect(before, after, StepReaction.STILL_CHANGING)

    assert summary == "{} new: - text: 250 ms".format(ui_flows.STILL_CHANGING_PREFIX)


def test_the_four_unchanged_sentences_are_told_apart_by_their_first_words() -> None:
    # The history is what the agent reads them from, and its rules key on how each one begins.
    sentences = [
        ui_flows.UNCHANGED_STATE_SUMMARY,
        ui_flows.NO_REACTION_SUMMARY,
        ui_flows.ACKNOWLEDGED_ONLY_SUMMARY,
        ui_flows.STILL_CHANGING_UNCHANGED_SUMMARY,
    ]

    assert len({sentence.split(":")[0] for sentence in sentences}) == len(sentences)
    assert ui_flows.NO_REACTION_SUMMARY.startswith("nothing happened")
    assert ui_flows.ACKNOWLEDGED_ONLY_SUMMARY.startswith("the page reacted but shows nothing new")


def test_the_prompt_exempts_a_scroll_from_the_rule_against_repeating_an_action() -> None:
    # A scroll's own effect is the viewport, which neither the DOM watch nor the accessible tree
    # shows, so every scroll on a page without lazy loading reports "nothing happened". Without the
    # exemption the rule above it would leave the agent one scroll per flow, and no way down a page.
    assert "'scroll' is the exception" in ui_flows._SYSTEM_PROMPT
    assert "Repeat it to reach further down the page" in ui_flows._SYSTEM_PROMPT


def test_wait_is_an_action_the_agent_can_choose_and_a_reader_can_name() -> None:
    action = ui_flows.parse_action({"action": "wait", "reasoning": "the page says saving", "expected": "it finishes"})

    assert action is not None and action.kind is ui_flows.FlowActionKind.WAIT
    assert ui_flows.describe_action(action) == "wait for the page to change"
    assert (
        json.loads(ui_flows.build_step_request(action, "", "http://127.0.0.1:1", "", ""))["action"]["kind"] == "wait"
    )


def test_the_prompt_tells_the_agent_when_to_wait_and_when_a_reload_counts_against_the_app() -> None:
    assert "choose 'wait'" in ui_flows._SYSTEM_PROMPT
    assert "Choose 'reload' only where the declared actions say to reload" in ui_flows._SYSTEM_PROMPT
    assert "wait: " in str(ui_flows._ACTION_TOOL["input_schema"])


def test_parse_step_result_carries_the_executors_word_on_the_dom() -> None:
    outcome = ui_flows.parse_step_result(
        json.dumps({"is_ok": True, "url": "https://x/", "title": "T", "snapshot": "- heading", "reaction": "none"})
    )

    assert outcome.reaction is StepReaction.NONE
    # A reply from a step that never watched carries the default rather than failing to parse.
    unwatched = ui_flows.parse_step_result(json.dumps({"is_ok": True, "url": "https://x/", "snapshot": "- h"}))
    assert unwatched.reaction is StepReaction.UNOBSERVED
