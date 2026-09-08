"""A scripted VerificationAgent for the evidence collector's flow-loop tests.

The flow loop's behaviour is a function of what the agent decides and what the step script reports, so a
scripted agent (paired with the scripted box environment) exercises every branch without an API
call. Actions and readings are consumed in order across the whole run; the last entry repeats, so a
test that only cares about the first few decisions does not have to pad the script to the step cap.
"""

from pydantic import Field

from imbue.minds_evals import ui_flows


class ScriptedVerificationAgent(ui_flows.VerificationAgent):
    """Returns canned decisions in order. A None entry stands for a call that produced nothing."""

    actions: list[ui_flows.FlowAction | None] = Field(default_factory=list, description="Decisions, in order")
    readings: list[ui_flows.FlowReading | None] = Field(default_factory=list, description="Readings, in order")
    action_count: int = Field(default=0, description="How many decisions have been handed out")
    reading_count: int = Field(default=0, description="How many readings have been handed out")
    prompts: list[str] = Field(default_factory=list, description="Every page state the agent was shown")

    def _record(self, is_answered: bool) -> ui_flows.VerifierCall:
        call = ui_flows.VerifierCall(
            tool_input={"scripted": True} if is_answered else None, input_token_count=100, output_token_count=20
        )
        self.calls.append(call)
        return call

    def decide_next_action(
        self, flow_steps: str, history: tuple[str, ...], state_text: str
    ) -> tuple[ui_flows.FlowAction | None, ui_flows.VerifierCall]:
        self.prompts.append(state_text)
        assert self.actions, "the scripted agent was asked for an action but has no script"
        action = self.actions[min(self.action_count, len(self.actions) - 1)]
        self.action_count += 1
        return action, self._record(action is not None)

    def read_final_state(
        self, flow_steps: str, history: tuple[str, ...], state_text: str
    ) -> tuple[ui_flows.FlowReading | None, ui_flows.VerifierCall]:
        assert self.readings, "the scripted agent was asked for a reading but has no script"
        reading = self.readings[min(self.reading_count, len(self.readings) - 1)]
        self.reading_count += 1
        return reading, self._record(reading is not None)


def done_action(reasoning: str = "every step is carried out") -> ui_flows.FlowAction:
    return ui_flows.FlowAction(
        kind=ui_flows.FlowActionKind.DONE, role="", target="", text="", amount=0, reasoning=reasoning
    )


def click_action(role: str = "button", target: str = "Add") -> ui_flows.FlowAction:
    return ui_flows.FlowAction(
        kind=ui_flows.FlowActionKind.CLICK, role=role, target=target, text="", amount=0, reasoning="clicking it"
    )


def reading(observation: str = "the final page lists the task") -> ui_flows.FlowReading:
    return ui_flows.FlowReading(observation=observation)
