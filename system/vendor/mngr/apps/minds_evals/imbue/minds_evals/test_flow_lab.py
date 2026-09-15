"""The flow lab against the todo fixture app.

The step script, the flow loop and the state summariser are exercised on a real Chromium against
page behaviours the fixture dials in through its query string, so what each of them reports for a
known behaviour is pinned here, along with how the lab's own browser launch fails. A test that
documents a defect the executor still has is marked as an expected failure against the issue that
tracks it, strictly: the day it passes, the mark comes off.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds_evals import flow_browser
from imbue.minds_evals import flow_lab
from imbue.minds_evals import flow_runner
from imbue.minds_evals import ui_flows
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.errors import FlowBrowserError
from imbue.minds_evals.mock_verification_agent_test import ScriptedVerificationAgent
from imbue.minds_evals.mock_verification_agent_test import done_action
from imbue.minds_evals.mock_verification_agent_test import reading
from imbue.minds_evals.resources.flow_step_protocol import StepReaction

# Every step spawns the step script and connects to the browser over CDP, so a flow of a handful
# of steps takes seconds rather than the project's ten-second default.
pytestmark = pytest.mark.timeout(120)

# The static apps the lab drives, one directory per app.
_FLOW_LAB_APPS_DIR = Path(__file__).parent.parent.parent / "flow_lab_apps"
_TODO_APP = _FLOW_LAB_APPS_DIR / "todo"
_ADD_COMPLETE_DELETE_STEPS = "Add a task named 'walk dog'. Mark it complete. Delete 'walk dog'."


def _executor(cdp_endpoint_url: str, group: ConcurrencyGroup, tmp_path: Path) -> flow_lab.LocalFlowStepExecutor:
    return flow_lab.LocalFlowStepExecutor(
        cdp_endpoint_url=cdp_endpoint_url, screenshot_dir=tmp_path / "frames", concurrency_group=group
    )


def _click(target: str, role: str = "button") -> ui_flows.FlowAction:
    return ui_flows.FlowAction(
        kind=ui_flows.FlowActionKind.CLICK,
        role=role,
        target=target,
        ref="",
        text="",
        amount=0,
        reasoning="the control is on the page",
        expected="the page reflects the click",
    )


def _click_ref(ref: str, role: str) -> ui_flows.FlowAction:
    return ui_flows.FlowAction(
        kind=ui_flows.FlowActionKind.CLICK,
        role=role,
        target="",
        ref=ref,
        text="",
        amount=0,
        reasoning="the control has no name, so its ref is the only handle",
        expected="the page reflects the click",
    )


def _ref_on(line: str) -> str:
    """The ref a snapshot line prints: `- checkbox [ref=e8]` gives `e8`."""
    return line.split("[ref=")[1].split("]")[0]


def _ref_of(state_text: str, line_marker: str) -> str:
    """The ref the captured state prints on the first line holding `line_marker`."""
    return _ref_on(next(line for line in state_text.splitlines() if line_marker in line))


def _checkbox_ref_of_row(state_text: str, task_text: str) -> str:
    """The ref of the nameless checkbox on the row whose text is `task_text`: the last checkbox
    the tree lists before that text."""
    checkbox_ref = ""
    for line in state_text.splitlines():
        if "- checkbox" in line:
            checkbox_ref = _ref_on(line)
        if task_text in line:
            return checkbox_ref
    raise AssertionError("no row holds {!r}".format(task_text))


def _type(text: str, target: str = "New task") -> ui_flows.FlowAction:
    return ui_flows.FlowAction(
        kind=ui_flows.FlowActionKind.INPUT,
        role="textbox",
        target=target,
        ref="",
        text=text,
        amount=0,
        reasoning="the field is on the page",
        expected="the field holds the text",
    )


def _reload() -> ui_flows.FlowAction:
    return ui_flows.FlowAction(
        kind=ui_flows.FlowActionKind.RELOAD,
        role="",
        target="",
        ref="",
        text="",
        amount=0,
        reasoning="checking persistence",
        expected="the page shows the same tasks",
    )


def _wait() -> ui_flows.FlowAction:
    return ui_flows.FlowAction(
        kind=ui_flows.FlowActionKind.WAIT,
        role="",
        target="",
        ref="",
        text="",
        amount=0,
        reasoning="the page says it is saving",
        expected="the save completes",
    )


def _effect(before: ui_flows.StepOutcome, after: ui_flows.StepOutcome) -> str:
    """What the loop would record for the step that took the page from `before` to `after`."""
    return ui_flows.summarize_step_effect(before.state_text, after.state_text, after.reaction)


async def _drive(
    executor: flow_lab.LocalFlowStepExecutor, url: str, actions: list[ui_flows.FlowAction]
) -> list[ui_flows.StepOutcome]:
    """Open `url` and perform `actions` in order, returning every capture, the opening's first."""
    outcomes = [await executor.run_step(flow_runner.opening_action(url), 0)]
    for index, action in enumerate(actions, start=1):
        outcomes.append(await executor.run_step(action, index))
    return outcomes


def _drive_todo(
    local_browser: str, group: ConcurrencyGroup, tmp_path: Path, page: str, actions: list[ui_flows.FlowAction]
) -> list[ui_flows.StepOutcome]:
    with flow_lab.serve_static_app(_TODO_APP) as origin:
        return asyncio.run(_drive(_executor(local_browser, group, tmp_path), origin + page, actions))


def test_a_browser_that_exits_before_serving_cdp_is_refused_by_name(
    flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    """A stub stands in for Chromium, so this runs on a machine with no browser installed -- which
    is the machine most likely to need a launch failure to be legible rather than a long wait."""
    stub_path = tmp_path / "not-chromium"
    stub_path.write_text("#!/bin/sh\nexit 3\n")
    stub_path.chmod(0o755)

    with pytest.raises(FlowBrowserError) as exc_info:
        with flow_browser.launch_local_browser(stub_path, tmp_path / "profile", flow_lab_group):
            pytest.fail("a browser that never started yielded a CDP endpoint")

    assert "exited with status 3" in str(exc_info.value)


def test_opening_the_app_captures_its_seed_tasks_and_a_frame(
    local_browser: str, flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    (opening,) = _drive_todo(local_browser, flow_lab_group, tmp_path, "", [])

    assert opening.is_ok, opening.detail
    assert opening.state_text.startswith("page http://127.0.0.1:")
    assert '"Buy milk"' in opening.state_text and '"Learn React"' in opening.state_text
    assert opening.screenshot_name == "step_000.png"
    assert (tmp_path / "frames" / "step_000.png").stat().st_size > 0


def test_a_synchronous_render_is_captured_by_the_step_that_caused_it(
    local_browser: str, flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    # With no latency the fixture mutates the DOM inside the click handler. The watch goes in
    # before the click, so a reaction that lands inside the handler is counted rather than missed.
    _opening, typed, added = _drive_todo(
        local_browser, flow_lab_group, tmp_path, "", [_type("walk dog"), _click("Add")]
    )

    assert '"walk dog"' in added.state_text
    assert added.reaction is StepReaction.SETTLED
    observed = _effect(typed, added)
    assert "new:" in observed and "walk dog" in observed


def test_a_deferred_render_is_captured_by_the_step_that_caused_it(
    local_browser: str, flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    # The fixture applies the add 300ms after the click, which is the shape of any framework that
    # batches updates. The capture recorded against the click shows its effect, because the step
    # waits for the page to start reacting and then to go quiet.
    _opening, typed, added = _drive_todo(
        local_browser, flow_lab_group, tmp_path, "?latency=300", [_type("walk dog"), _click("Add")]
    )

    assert '"walk dog"' in added.state_text
    assert added.reaction is StepReaction.SETTLED
    assert "walk dog" in _effect(typed, added)


def test_a_dead_control_reads_as_nothing_happening(
    local_browser: str, flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    # The Refresh button does nothing at all. The watch's cap runs out with no mutation, which is a
    # positive signal -- the page did not react -- rather than a page that merely looks the same.
    opening, refreshed = _drive_todo(local_browser, flow_lab_group, tmp_path, "", [_click("Refresh")])

    assert refreshed.is_ok, refreshed.detail
    assert refreshed.reaction is StepReaction.NONE
    assert _effect(opening, refreshed) == ui_flows.NO_REACTION_SUMMARY


def test_a_page_that_never_goes_quiet_is_read_with_a_warning_that_it_had_not_settled(
    local_browser: str, flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    # A clock repainting every 100ms never lets the DOM settle, so the watch gives up at its cap and
    # says the page keeps changing on its own. The clock's repaint is still the only movement the
    # tree has, so it is still what the summary names -- the prefix is what tells the agent that the
    # state it is about to act on was moving as it was read. That floor is what an agent's "did
    # anything happen?" sits on in any app with a timer, a poll or an animation.
    opening, refreshed = _drive_todo(local_browser, flow_lab_group, tmp_path, "?ticker=1", [_click("Refresh")])

    assert refreshed.is_ok, refreshed.detail
    assert refreshed.reaction is StepReaction.STILL_CHANGING
    observed = _effect(opening, refreshed)
    assert observed.startswith(ui_flows.STILL_CHANGING_PREFIX)
    # Whatever the clock happened to read, not the values it read.
    assert all(line.endswith(" ms") for line in observed.splitlines()), observed


def test_an_add_the_app_silently_dedupes_shows_only_the_textbox_clearing(
    local_browser: str, flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    # The app drops a case-insensitive duplicate of a seed task without saying so. No row is added,
    # and the only movement the summary can name is the textbox giving up its text -- which is the
    # app's one tell that the click was taken at all.
    _opening, typed, added = _drive_todo(
        local_browser, flow_lab_group, tmp_path, "?dedupe=ci", [_type("buy milk"), _click("Add")]
    )

    assert added.state_text.count('"Buy milk"') == 1 and 'checkbox "buy milk"' not in added.state_text
    observed = _effect(typed, added)
    assert observed != ui_flows.UNCHANGED_STATE_SUMMARY
    assert all('textbox "New task"' in line for line in observed.splitlines())


def test_arming_a_delete_button_reads_as_acknowledged_without_a_visible_change(
    local_browser: str, flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    # The first click arms the button: a highlight and focus, no row removed. The accessible tree is
    # unchanged, but the watch saw the page react (the class it set), so the agent is told the
    # control acknowledged the click rather than that nothing happened -- and a second click is
    # what this control wants.
    opening, armed, deleted = _drive_todo(
        local_browser,
        flow_lab_group,
        tmp_path,
        "?arm_delete=1",
        [_click('Delete "Buy milk"'), _click('Delete "Buy milk"')],
    )

    assert '"Buy milk"' in armed.state_text
    assert armed.reaction is StepReaction.SETTLED
    assert _effect(opening, armed) == ui_flows.ACKNOWLEDGED_ONLY_SUMMARY
    assert '"Buy milk"' not in deleted.state_text


def test_typing_the_app_does_not_answer_still_shows_the_field_it_filled(
    local_browser: str, flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    # The field's text is the typing's own effect and the tree shows it, so a tree that moved speaks
    # for itself even where the DOM never reacted. (The app reacting further is a bonus, which is why
    # typing waits on the shorter cap.)
    opening, typed = _drive_todo(local_browser, flow_lab_group, tmp_path, "", [_type("walk dog")])

    assert typed.reaction is StepReaction.NONE
    observed = _effect(opening, typed)
    assert "walk dog" in observed and observed != ui_flows.NO_REACTION_SUMMARY


def test_a_pending_state_is_what_the_step_captures_and_a_wait_is_what_resolves_it(
    local_browser: str, flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    # The app answers the click at once with "Saving..." and only applies the add later. The click's
    # capture is that pending state -- the page settled on it -- and the wait action, performing
    # nothing, gives the page its time and captures the result. The window has to outlast the click's
    # capture AND the spawn of the next step's process, or the add would land before the wait started
    # watching for it, so it is set well above what that round trip costs.
    _opening, typed, added, waited = _drive_todo(
        local_browser, flow_lab_group, tmp_path, "?pending=4000", [_type("walk dog"), _click("Add"), _wait()]
    )

    assert "Saving..." in added.state_text and 'checkbox "walk dog"' not in added.state_text
    assert added.reaction is StepReaction.SETTLED
    assert "Saving..." in _effect(typed, added)
    assert 'checkbox "walk dog"' in waited.state_text and "Saving..." not in waited.state_text
    assert waited.reaction is StepReaction.SETTLED
    assert "walk dog" in _effect(added, waited)


def test_a_wait_on_a_page_that_has_nothing_pending_says_nothing_happened(
    local_browser: str, flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    opening, waited = _drive_todo(local_browser, flow_lab_group, tmp_path, "", [_wait()])

    assert waited.reaction is StepReaction.NONE
    assert _effect(opening, waited) == ui_flows.NO_REACTION_SUMMARY


def test_a_click_that_navigates_is_read_once_the_new_page_has_loaded(
    local_browser: str, flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    # A plain link leaves the document as the click is handled, so the watch's world is already gone
    # when the watch is asked what it saw. The step reads that as the reaction it is -- the new page,
    # once its network has settled -- rather than as the watch failing.
    opening, navigated = _drive_todo(
        local_browser, flow_lab_group, tmp_path, "?latency=300", [_click("Start over", role="link")]
    )

    assert navigated.is_ok, navigated.detail
    assert navigated.reaction is StepReaction.SETTLED
    assert "?latency=300" in opening.state_text.splitlines()[0]
    assert "?latency=300" not in navigated.state_text.splitlines()[0]
    assert '"Buy milk"' in navigated.state_text


def test_a_navigation_that_lands_while_the_watch_is_waiting_is_read_the_same_way(
    local_browser: str, flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    # With `pending` the link answers the click with a status and only then leaves the document, the
    # shape of an app that saves and redirects; the clock keeps the DOM moving so the watch is still
    # waiting on the page when that happens. The browser reports a world that dies under a waiting
    # evaluation differently from one that was already gone, and both mean the same thing, so the
    # step must read this one as the new page too rather than as the executor breaking.
    _opening, navigated = _drive_todo(
        local_browser, flow_lab_group, tmp_path, "?ticker=1&pending=700", [_click("Start over", role="link")]
    )

    assert navigated.is_ok, navigated.detail
    assert navigated.reaction is StepReaction.SETTLED
    assert "pending=700" not in navigated.state_text.splitlines()[0]
    assert '"Buy milk"' in navigated.state_text


def test_a_nameless_control_is_addressed_by_its_ref_and_the_next_step_reads_fresh_refs(
    local_browser: str, flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    # Under ?unnamed=1 a task's checkbox has no accessible name: the tree lists a bare `checkbox`,
    # and the ref it prints is the only handle the page gives. The second ref is read off the state
    # the ref step itself captured, which is what pins that a capture after a ref lookup numbers the
    # page the way the next step's own lookup will.
    executor = _executor(local_browser, flow_lab_group, tmp_path)
    with flow_lab.serve_static_app(_TODO_APP) as origin:

        async def drive() -> list[ui_flows.StepOutcome]:
            outcomes = await _drive(executor, origin + "?unnamed=1", [_type("walk dog"), _click("Add")])
            checkbox_ref = _checkbox_ref_of_row(outcomes[-1].state_text, "walk dog")
            outcomes.append(await executor.run_step(_click_ref(checkbox_ref, "checkbox"), 3))
            completed = outcomes[-1].state_text
            outcomes.append(await executor.run_step(_click_ref(_ref_of(completed, 'Delete \\"walk dog'), "button"), 4))
            return outcomes

        outcomes = asyncio.run(drive())

    added, completed, deleted = outcomes[2], outcomes[3], outcomes[4]
    assert "- checkbox [ref=" in added.state_text and 'checkbox "' not in added.state_text
    assert (completed.is_ok, completed.reaction) == (True, StepReaction.SETTLED), completed.detail
    assert "[checked]" in completed.state_text and "walk dog" in completed.state_text
    assert (deleted.is_ok, deleted.reaction) == (True, StepReaction.SETTLED), deleted.detail
    assert "walk dog" not in deleted.state_text


def test_a_ref_that_no_longer_names_what_the_agent_read_is_a_step_error_the_flow_survives(
    local_browser: str, flow_lab_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    # A ref is checked against a fresh snapshot before it is acted on: one that sits on another
    # role now, or on nothing, is refused with the page captured as it stands, so the flow carries
    # on from the current state rather than clicking whatever inherited the number.
    outcomes = _drive_todo(local_browser, flow_lab_group, tmp_path, "?unnamed=1", [])
    checkbox_ref = _ref_of(outcomes[0].state_text, "- checkbox")

    executor = _executor(local_browser, flow_lab_group, tmp_path)
    moved = asyncio.run(executor.run_step(_click_ref(checkbox_ref, "button"), 1))
    gone = asyncio.run(executor.run_step(_click_ref("e999", "checkbox"), 2))

    assert (moved.is_ok, moved.reason) == (False, ui_flows.REASON_STALE_REF)
    assert not ui_flows.is_instrument_reason(moved.reason)
    assert "names a checkbox now, not a button" in moved.detail
    assert (gone.is_ok, gone.reason) == (False, ui_flows.REASON_STALE_REF)
    assert "e999 is not on the page any more" in gone.detail
    # The page below is still captured, and still the page the flow can go on from.
    assert "Buy milk" in moved.state_text and "Buy milk" in gone.state_text


def test_an_unusable_decision_is_recorded_as_a_step_that_did_not_run(chromium_path: Path, tmp_path: Path) -> None:
    # The flow ends on the instrument, but the log still shows the page the decision was made on
    # and why nothing could be done with it, rather than stopping after a step that worked.
    agent = ScriptedVerificationAgent(actions=[None], readings=[reading()])
    output_dir = tmp_path / "flow"

    run = asyncio.run(
        flow_lab.run_lab_flow(
            app_dir=_TODO_APP,
            page="",
            check=flow_lab.lab_flow_check("add_complete_delete", _ADD_COMPLETE_DELETE_STEPS, "'walk dog' is gone"),
            agent=agent,
            output_dir=output_dir,
            chromium_path=chromium_path,
        )
    )

    assert (run.status, run.reason) == (CheckStatus.ERROR, ui_flows.REASON_VERIFIER_AGENT_FAILED)
    assert run.detail == "the verification agent returned no usable action: the model call produced no tool payload"
    records = [json.loads(line) for line in (output_dir / "log.jsonl").read_text().splitlines()]
    assert [record["kind"] for record in records] == ["init", "action"]
    assert records[1]["action"] == ui_flows.UNUSABLE_ACTION
    assert records[1]["error"] == run.detail
    assert "Buy milk" in records[1]["state"]


def test_a_scripted_flow_runs_to_completion_and_leaves_a_trials_evidence(chromium_path: Path, tmp_path: Path) -> None:
    # The whole loop -- opening, deciding, acting, summarising, reading -- against the fixture, with
    # the agent's decisions scripted so the record is a function of the executor alone.
    agent = ScriptedVerificationAgent(
        actions=[
            _type("walk dog"),
            _click("Add"),
            _click("walk dog", role="checkbox"),
            _click('Delete "walk dog"'),
            _reload(),
            done_action(),
        ],
        readings=[reading("the list holds Buy milk and Learn React; walk dog is gone")],
    )
    output_dir = tmp_path / "flow"

    run = asyncio.run(
        flow_lab.run_lab_flow(
            app_dir=_TODO_APP,
            page="",
            check=flow_lab.lab_flow_check("add_complete_delete", _ADD_COMPLETE_DELETE_STEPS, "'walk dog' is gone"),
            agent=agent,
            output_dir=output_dir,
            chromium_path=chromium_path,
        )
    )

    assert (run.status, run.reason) == (CheckStatus.PASSED, "")
    records = [json.loads(line) for line in (output_dir / "log.jsonl").read_text().splitlines()]
    assert [record["kind"] for record in records] == ["init", *["action"] * 6, "final"]
    observed_by_step = {record["step_index"]: record["observed"] for record in records if record["kind"] == "action"}
    assert "walk dog" in observed_by_step[2] and "new:" in observed_by_step[2]
    assert "walk dog" in observed_by_step[4] and "gone:" in observed_by_step[4]
    # A reload is a navigation, which is not watched: the tree is unchanged and that is all the
    # record can say.
    assert observed_by_step[5] == ui_flows.UNCHANGED_STATE_SUMMARY
    reaction_by_step = {record["step_index"]: record["reaction"] for record in records if record["kind"] == "action"}
    assert reaction_by_step == {1: "none", 2: "settled", 3: "settled", 4: "settled", 5: "unobserved", 6: "unobserved"}
    assert sorted(path.name for path in output_dir.glob("*.png")) == [
        ui_flows.flow_screenshot_name(index) for index in range(6)
    ]
    assert [len(history) for history in agent.histories] == [0, 1, 2, 3, 4, 5]


@pytest.mark.release
# Unlike the scripted tests, this one's length is the agent's: up to MAX_STEPS_PER_FLOW decisions
# plus a closing reading, each a live model call. Sized from the deadline the loop itself enforces,
# so a stuck run is stopped by the flow deadline -- with a reason and a full record -- rather than
# by pytest's clock, and the grace covers the browser launch and the static server outside it.
@pytest.mark.timeout(flow_runner.FLOW_DEADLINE_SECONDS + 60)
@pytest.mark.parametrize("page", ["?latency=300", "?pending=2000", "?unnamed=1"])
def test_the_real_agent_completes_the_flow_without_reloading(chromium_path: Path, tmp_path: Path, page: str) -> None:
    """Three shapes of a real app: one that reacts 300ms after each click (the matrix smoke runs on
    PR #900), one that answers each click with a pending state and applies it two seconds later, and
    one whose checkbox has no accessible name. All three are the declared actions carried out with
    no reload, which the flow did not ask for and which a waiting agent does not need; the last is
    also the nameless control addressed by its ref rather than the agent giving up on it.

    A run needs ANTHROPIC_API_KEY, spends a few model calls, and is not deterministic, so it is a
    release test rather than a per-PR one.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY is not set; the real verification agent cannot be run")
    agent = ui_flows.AnthropicVerificationAgent(
        model="claude-haiku-4-5-20251001",
        api_key=SecretStr(api_key),
        timeout_seconds=ui_flows.DEFAULT_CALL_TIMEOUT_SECONDS,
    )

    run = asyncio.run(
        flow_lab.run_lab_flow(
            app_dir=_TODO_APP,
            page=page,
            check=flow_lab.lab_flow_check("add_complete_delete", _ADD_COMPLETE_DELETE_STEPS, "'walk dog' is gone"),
            agent=agent,
            output_dir=tmp_path / "flow",
            chromium_path=chromium_path,
        )
    )

    assert (run.status, run.reason) == (CheckStatus.PASSED, ""), run.detail
    records = [json.loads(line) for line in run.records if json.loads(line)["kind"] == "action"]
    actions = [record["action"] for record in records]
    assert "reload the page" not in actions, actions
    if page == "?unnamed=1":
        assert any(record["target_ref"] for record in records), actions
