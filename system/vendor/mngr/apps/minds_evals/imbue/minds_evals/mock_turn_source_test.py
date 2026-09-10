"""Concrete scripted TurnSource implementations for driver unit tests, plus a driver subclass that
uses them. The conversation loop is exercised end to end this way -- real send/wait/record path, no
model calls -- which is what the real sources make impossible in a unit test."""

from typing import Any

from pydantic import Field

from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import DeciderResult
from imbue.minds_evals.data_types import Transcript
from imbue.minds_evals.data_types import TurnEntryKind
from imbue.minds_evals.data_types import TurnOutcome
from imbue.minds_evals.driver import Done
from imbue.minds_evals.driver import MindsPersonaDriver
from imbue.minds_evals.driver import Say
from imbue.minds_evals.driver import TurnAction
from imbue.minds_evals.driver import TurnSource


class ScriptedTurnSource(TurnSource):
    """Replays a fixed list of actions, recording the transcript it was shown for each one.

    Repeats the final action once the script runs out, so a test can assert that the LOOP -- not the
    source -- is what stops an entry at its budget.
    """

    actions: list[TurnAction] = Field(description="The actions to return, in order")
    entry_kind: TurnEntryKind = Field(description="The kind this source reports itself as")
    budget_outcome: TurnOutcome = Field(description="What the loop should record if the budget stops it")
    # One decider result appended per action, so the driver's audit-event and usage accounting paths
    # run exactly as they do for a real model-backed source -- which bills the call that decided to
    # stop just like the ones that spoke.
    is_decider_call_simulated: bool = Field(default=False, description="Whether each action reports a model call")
    seen_conversations: list[str] = Field(
        default_factory=list, description="The rendered conversation the source was shown, per call"
    )
    call_count: int = Field(default=0, description="How many times the loop asked for an action")

    @property
    def kind(self) -> TurnEntryKind:
        return self.entry_kind

    @property
    def exhaustion_end(self) -> Done:
        return Done(reason=self.budget_outcome)

    def next_action(self, case: CaseConfig, transcript: Transcript) -> TurnAction:
        self.seen_conversations.append(
            " | ".join(str(event.get("content") or event.get("text") or "") for event in transcript.events)
        )
        action = self.actions[min(self.call_count, len(self.actions) - 1)]
        self.call_count += 1
        if self.is_decider_call_simulated:
            self.results.append(
                DeciderResult(
                    message=action.text if isinstance(action, Say) else "",
                    model="scripted-model",
                    input_token_count=11,
                    output_token_count=7,
                    is_fallback=False,
                )
            )
        return action


class ScriptedSourceDriver(MindsPersonaDriver):
    """The real driver with its turn sources supplied by the test instead of resolved from the case."""

    def __init__(self, scripted_sources: list[TurnSource], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._scripted_sources = scripted_sources

    def build_turn_sources(self, case: CaseConfig) -> list[TurnSource]:
        assert len(self._scripted_sources) == len(case.prompts), "the script must supply one source per prompts entry"
        return self._scripted_sources


def say(text: str) -> Say:
    return Say(text=text)


def done(reason: TurnOutcome, detail: str = "") -> Done:
    return Done(reason=reason, detail=detail)
