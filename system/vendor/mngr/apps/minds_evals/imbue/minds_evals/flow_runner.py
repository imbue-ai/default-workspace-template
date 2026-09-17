"""One UI flow, from its opening navigation to its closing reading, driven through an executor.

The loop is the same wherever the browser lives: open the app, then read the page, ask the
verification agent for one action, perform it and record what the page did with it, until the
agent says it is done or a budget runs out. What differs is who performs a step -- at trial time the
box's one-shot step script, exec'd over the harbor bridge; in the flow lab a local Chromium -- so
the loop takes the executor as an interface and returns the flow's whole record as data. The
evidence collector writes that record where the grade-time verifier reads it; the lab writes it to a
directory of its own. Neither rules on the flow's `expect`: that stays the judge's.
"""

import time
from abc import ABC
from abc import abstractmethod
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals import ui_flows
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.data_types import UiFlowCheck
from imbue.minds_evals.resources.flow_step_protocol import StepReaction

# How much of a failed step's error text rides in the flow log and the agent's history.
MAX_STEP_ERROR_CHARS: Final[int] = 200

# One flow step, from handing the executor an action to reading its reply. Generous because a
# navigation waits for the network to settle and a heavy page's ARIA tree is large. Every executor
# spends the same budget, so a flow reproduced in the lab is bounded the way the box bounds it.
STEP_TIMEOUT_SECONDS: Final[int] = 120

# One flow's own wall-clock, agent calls included. Separate from any phase budget on purpose:
# exceeding this is the app failing to respond, whereas exhausting a phase budget is the harness
# running out of time. Measured against the executor that performs the steps, so it is only a valid
# bound while every executor spends about what the box's does per step.
FLOW_DEADLINE_SECONDS: Final[float] = 600.0


class FlowStepExecutor(MutableModel, ABC):
    """Performs one decided action in the browser a flow is driving, and reads the page back.

    An interface so the loop can drive a box-side browser over the harbor bridge at trial time and a
    local one in the flow lab with the same code. The executor owns everything about WHERE the
    browser is -- its CDP endpoint, where a step's frame is written, the session cookie the opening
    request carries -- and the loop owns everything about WHAT happens: which action, in which order,
    and what is recorded about it.
    """

    @abstractmethod
    async def run_step(self, action: ui_flows.FlowAction, step_index: int) -> ui_flows.StepOutcome:
        """Perform `action` and capture the page after it. Step 0 is the flow's opening navigation."""


class FlowRun(FrozenModel):
    """What one flow produced: its verdict on completion, and every line of its log."""

    status: CheckStatus = Field(
        description="PASSED when the agent finished the declared actions, else FAILED or ERROR"
    )
    reason: str = Field(description="Why the flow did not complete, empty when it did")
    detail: str = Field(description="Bounded prose about the outcome, for the manifest entry")
    records: tuple[str, ...] = Field(description="The flow's log.jsonl lines, in order")


@pure
def opening_action(target_url: str) -> ui_flows.FlowAction:
    """The navigation every flow starts with, before any action is decided."""
    return ui_flows.FlowAction(
        kind=ui_flows.FlowActionKind.OPEN,
        role="",
        target="",
        ref="",
        text=target_url,
        amount=0,
        reasoning="the flow has not opened the app yet",
        expected="the delivered app loads",
    )


async def run_flow(
    check: UiFlowCheck,
    target_url: str,
    agent: ui_flows.VerificationAgent,
    executor: FlowStepExecutor,
    phase_deadline: float,
    flow_deadline: float,
) -> FlowRun:
    """Open the app, then read-decide-act until the agent says it is done, and close with the
    agent's reading of the final state.

    Both deadlines are monotonic-clock instants. The phase deadline is the harness running out of
    time, and reaching it is an ERROR; the flow deadline is about THIS app -- a page that never
    settles is the delivered thing being unusable -- so reaching it is a FAILURE charged to the app.
    """
    records: list[str] = []
    history: list[str] = []

    outcome = await executor.run_step(opening_action(target_url), 0)
    if not outcome.is_ok:
        # The harness's own navigation to the app. If THIS fails on the instrument, it cannot look at
        # the app at all; if it fails on the app -- a page that never loads -- that is the
        # deliverable falling short. A step that failed always names its layer; a report that names
        # none is the executor failing to say what happened, which is an instrument failure like
        # any other. The status follows the reason, so the two can never disagree.
        reason = outcome.reason or ui_flows.REASON_STEP_ERROR
        return FlowRun(
            status=CheckStatus.ERROR if ui_flows.is_instrument_reason(reason) else CheckStatus.FAILED,
            reason=reason,
            detail=outcome.detail,
            records=(),
        )
    state_text = outcome.state_text
    records.append(
        ui_flows.flow_init_record(
            check.actions, check.expect, target_url, state_text, outcome.screenshot_name, ui_flows.utc_now_iso()
        )
    )

    is_finished_by_agent = False
    for step_index in range(1, ui_flows.MAX_STEPS_PER_FLOW + 1):
        if time.monotonic() >= phase_deadline:
            return FlowRun(
                status=CheckStatus.ERROR,
                reason=ui_flows.REASON_TIMEOUT,
                detail="the collection budget ran out mid-flow",
                records=tuple(records),
            )
        if time.monotonic() >= flow_deadline:
            return FlowRun(
                status=CheckStatus.FAILED,
                reason=ui_flows.REASON_FLOW_DEADLINE,
                detail="the flow did not finish within its deadline",
                records=tuple(records),
            )
        action, call = agent.decide_next_action(check.actions, tuple(history), state_text)
        if action is None:
            # Recorded as a step that did not run, so the log shows the page the decision was made
            # on and what the decision asked for; the reader otherwise sees a flow that stopped
            # after a step that worked, with the manifest naming only the layer.
            detail = "the verification agent returned no usable action: {}".format(
                ui_flows.describe_unusable_action(call.tool_input)
            )
            logger.warning("The verification agent's decision could not be acted on: {}", detail)
            payload = call.tool_input or {}
            records.append(
                ui_flows.flow_step_record(
                    step_index,
                    ui_flows.UNUSABLE_ACTION,
                    "",
                    str(payload.get("reasoning") or "").strip(),
                    str(payload.get("expected") or "").strip(),
                    "",
                    StepReaction.UNOBSERVED,
                    state_text,
                    "",
                    detail,
                    ui_flows.utc_now_iso(),
                )
            )
            return FlowRun(
                status=CheckStatus.ERROR,
                reason=ui_flows.REASON_VERIFIER_AGENT_FAILED,
                detail=detail,
                records=tuple(records),
            )
        described = ui_flows.describe_action(action)
        if action.kind == ui_flows.FlowActionKind.DONE:
            records.append(
                ui_flows.flow_step_record(
                    step_index,
                    described,
                    action.ref,
                    action.reasoning,
                    action.expected,
                    "",
                    StepReaction.UNOBSERVED,
                    state_text,
                    "",
                    "",
                    ui_flows.utc_now_iso(),
                )
            )
            is_finished_by_agent = True
            break
        outcome = await executor.run_step(action, step_index)
        if ui_flows.is_instrument_reason(outcome.reason):
            records.append(
                ui_flows.flow_step_record(
                    step_index,
                    described,
                    action.ref,
                    action.reasoning,
                    action.expected,
                    "",
                    StepReaction.UNOBSERVED,
                    state_text,
                    "",
                    outcome.reason,
                    ui_flows.utc_now_iso(),
                )
            )
            return FlowRun(
                status=CheckStatus.ERROR, reason=outcome.reason, detail=outcome.detail, records=tuple(records)
            )
        step_error = ""
        if not outcome.is_ok:
            # The action did not land but the browser is fine -- an element that is not there, a
            # click that hit nothing. The page below shows the truth, so the flow carries on with
            # the failure recorded where the grade-time judge will read it.
            step_error = ui_flows.bounded_tail(outcome.detail.strip(), MAX_STEP_ERROR_CHARS)
        # What the page did, against what the action predicted it would do. Recorded on the step and
        # carried into the next decision's history, which is where a wrong model of the UI -- a
        # filter that turns out to be a toggle -- becomes visible instead of being retried.
        observed = ui_flows.summarize_step_effect(state_text, outcome.state_text or state_text, outcome.reaction)
        history.append(ui_flows.describe_step(described, action.expected, step_error, observed))
        records.append(
            ui_flows.flow_step_record(
                step_index,
                described,
                action.ref,
                action.reasoning,
                action.expected,
                observed,
                outcome.reaction,
                state_text,
                # The executor names the frame it actually wrote, and names nothing when the capture
                # failed. Naming the file it would have written instead would put a screenshot that
                # does not exist in front of the grade-time judge.
                outcome.screenshot_name,
                step_error,
                ui_flows.utc_now_iso(),
            )
        )
        state_text = outcome.state_text or state_text

    # The agent's account of the state the flow ended in. Evidence for the judge, never a verdict on
    # the `expect` -- and a call that produced nothing costs the flow its context, not its
    # completion, because the log already carries every state that was seen.
    reading, _reading_call = agent.read_final_state(check.actions, tuple(history), state_text)
    observation = reading.observation if reading is not None else ""
    records.append(ui_flows.flow_final_record(len(records), observation, state_text, ui_flows.utc_now_iso()))
    # Completion, not achievement: a flow that carried out its declared actions is `passed`, and one
    # that ran out of budget first is `failed`. Whether the app did what the `expect` describes is
    # decided at grade time, from this record.
    return FlowRun(
        status=CheckStatus.PASSED if is_finished_by_agent else CheckStatus.FAILED,
        reason="" if is_finished_by_agent else ui_flows.REASON_STEP_BUDGET_EXHAUSTED,
        detail="expected: {}\nagent's reading of the final state: {}".format(
            check.expect, observation or "(none recorded)"
        ),
        records=tuple(records),
    )
