"""The verification agent: the reasoning half of UI-flow execution, plus the executor it drives.

A declared flow is natural language ("Add a task named 'buy milk'. Reload the page."), and the app
it runs against was invented by the agent under test, so there are no selectors to script. This
module is the decider's sibling: it renders the flow plus what the browser currently sees into a
prompt, asks a model for the single next browser action, and -- once the steps are done -- asks it
whether the flow's `expect` holds. Everything it returns is data; the loop that executes actions and
writes evidence lives in the evidence collector, exactly as the decider's loop lives in the driver.

Reasoning stays host-side on purpose: a loop delegated to the browser would return a bare claim
rather than a stepwise record, and could be neither bounded nor observed. Here every step is
budgeted, logged, and billed to harness spend.

The other half of this module turns decided actions into requests for the box-side executor -- a
headless Chromium driving the app's forwarded origin, its own label on the workspace's agent-keyed
origin, where the proxy serves it -- and classifies what comes back. That classification is what lets a broken instrument (no browser,
no proxy, a dead tunnel, refused TLS) be recorded as ERROR while a broken app is recorded as
FAILED.
"""

import json
import re
import shlex
from abc import ABC
from abc import abstractmethod
from collections import Counter
from collections.abc import Sequence
from enum import auto
from typing import Any
from typing import Final
from typing import assert_never

from anthropic.types import ToolParam
from pydantic import Field
from pydantic import SecretStr
from pydantic import ValidationError

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals import minds_bridge
from imbue.minds_evals import model_calls
from imbue.minds_evals.forward_instance import SESSION_COOKIE_NAME
from imbue.minds_evals.resources import flow_step_protocol
from imbue.minds_evals.resources.flow_step_protocol import StepAction
from imbue.minds_evals.resources.flow_step_protocol import StepActionKind
from imbue.minds_evals.resources.flow_step_protocol import StepCookie
from imbue.minds_evals.resources.flow_step_protocol import StepRequest
from imbue.minds_evals.resources.flow_step_protocol import StepResult

MAX_TOKENS: Final[int] = 1024
# Per-call HTTP timeout. Generous: a stalled call costs the flow its budget, not the whole phase.
DEFAULT_CALL_TIMEOUT_SECONDS: Final[float] = 120.0

# How much of one page state reaches the model. A large page's accessibility tree runs to tens of
# thousands of tokens, and the head of it -- the URL, the title, and the top of the tree -- is what
# an action is chosen from.
MAX_STATE_PROMPT_CHARS: Final[int] = 12_000
# How much of the prompt the history may take. It grows with every step while the page state above
# is capped, so without a budget of its own a long flow ends up reasoning mostly about what it
# already did rather than about the page in front of it.
MAX_HISTORY_PROMPT_CHARS: Final[int] = 4_000

# A state summary is for orienting the next decision, not for reading the page: past these it
# says how much moved and stops, because a hundred changed lines in the prompt crowd out the page
# state the next action is actually chosen from.
MAX_SUMMARY_LINES: Final[int] = 6
MAX_SUMMARY_LINE_CHARS: Final[int] = 120
# Beyond this share of the tree the page has effectively been replaced -- a navigation, a modal over
# everything -- and naming individual lines describes nothing. Guarded by a floor, because a share
# alone would call every real change on a short page a replacement.
MAX_SUMMARY_CHANGE_RATIO: Final[float] = 0.4
MIN_REPLACEMENT_LINES: Final[int] = 2 * MAX_SUMMARY_LINES

# How much of that budget the TAIL of a cut state keeps. Overlays and detail panels are appended to
# the end of the document (React portals render at the end of body), so a head-only cut hides
# exactly the element the agent's last action opened and it loops re-opening it.
TAIL_STATE_PROMPT_CHARS: Final[int] = 5_000

_ACTION_TOOL_NAME: Final[str] = "next_browser_action"
_READING_TOOL_NAME: Final[str] = "flow_reading"


class FlowRecordKind(LowerCaseStrEnum):
    """Which kind of line a flow's log.jsonl holds.

    Every line carries one, so a reader dispatches on the kind rather than inferring from the
    fields present. A reader that meets a kind it does not know shows the record as it stands
    rather than dropping it, which is what lets a new kind be added without coordinating readers.
    """

    # The flow before it acted: what it was asked to do, and the page it opened onto.
    INIT = auto()
    ACTION = auto()
    # The agent's account of the state the flow ended in.
    FINAL = auto()


class FlowActionKind(LowerCaseStrEnum):
    """What the verification agent asked the browser to do next."""

    CLICK = auto()
    INPUT = auto()
    KEYS = auto()
    SCROLL = auto()
    OPEN = auto()
    # Its own action rather than a re-`open`: a reload keeps the URL and the session, which is
    # exactly what a persistence flow is testing, whereas navigating afresh would not.
    RELOAD = auto()
    # The declared steps are complete, or the agent cannot make further progress; either way the
    # `expect` is then evaluated against whatever state the page is in.
    DONE = auto()


class FlowAction(FrozenModel):
    """One decided browser action, before it becomes a step request for the box-side browser.

    Elements are addressed by ACCESSIBLE ROLE AND NAME rather than by an index into a listing.
    That is what the page snapshot itself is expressed in, and it survives the page changing
    underneath the agent -- an index does not, which is why an index-addressed executor has to re-read the
    page before every single action just to keep its numbering valid.
    """

    kind: FlowActionKind = Field(description="Which browser operation to perform")
    role: str = Field(description="The target element's ARIA role, e.g. 'button' or 'textbox'")
    target: str = Field(description="The target element's accessible name")
    text: str = Field(description="Text to type, keys to press, or the URL to open")
    amount: int = Field(description="Scroll distance in pixels (negative scrolls up)")
    reasoning: str = Field(description="Why the agent chose this action, recorded in the flow log")
    # The prediction is what `reasoning` alone could not give: the next decision is shown it beside
    # what the page actually did, so a wrong model of the UI shows up as a contradiction instead of
    # being re-derived every turn.
    expected: str = Field(description="What the agent expects the action to do, in one sentence")


class FlowReading(FrozenModel):
    """What the agent says the page finally showed -- an observation, recorded as evidence.

    Deliberately not a judgement on the flow's `expect`. Trial time collects; the grade-time judge
    is the one that rules on whether the expectation holds, from the step log and the screenshots.
    A boolean here would be a second verdict on the same question, and the one made with less to go
    on.
    """

    observation: str = Field(description="What the final page state shows, in the agent's words")


class VerifierUsage(FrozenModel):
    """What the verification agent itself consumed. Harness spend, reported next to the decider's
    and never folded into the workspace agent's cost."""

    model: str = Field(description="The model the verification agent ran on")
    call_count: int = Field(description="Model calls made across every flow")
    failed_call_count: int = Field(description="Calls that raised or returned nothing usable")
    input_token_count: int = Field(description="Input tokens across those calls")
    output_token_count: int = Field(description="Output tokens across those calls")


_ACTION_TOOL: Final[ToolParam] = {
    "name": _ACTION_TOOL_NAME,
    "description": "Perform the next browser action in the flow, or finish the flow.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": "One sentence: what you see and why this action is the next step.",
            },
            "expected": {
                "type": "string",
                "description": (
                    "One sentence: what you expect this action to change on the page. Be specific "
                    "enough that the next turn can tell whether it happened."
                ),
            },
            "action": {
                "type": "string",
                "enum": [member.value for member in FlowActionKind],
                "description": (
                    "click: click the element named by role + target. "
                    "input: type text into the element named by role + target -- any editable "
                    "element, not only textboxes (headings and cells are often editable in place). "
                    "keys: press a key combination (e.g. 'Enter'). "
                    "scroll: scroll the page by amount pixels. "
                    "open: navigate to the url in text. "
                    "reload: reload the current page, keeping the session. "
                    "done: every declared step is complete, or no further progress is possible."
                ),
            },
            "role": {
                "type": "string",
                "description": "The target element's ARIA role exactly as the page snapshot spells it, e.g. 'button', 'textbox', 'checkbox', 'link'.",
            },
            "target": {
                "type": "string",
                "description": "The target element's accessible name, exactly as the page snapshot spells it.",
            },
            "text": {"type": "string", "description": "Text to type, keys to press, or the URL to open."},
            "amount": {"type": "integer", "description": "Scroll distance in pixels; negative scrolls up."},
        },
        "required": ["reasoning", "expected", "action"],
    },
}

_READING_TOOL: Final[ToolParam] = {
    "name": _READING_TOOL_NAME,
    "description": "Describe what the page finally showed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "observation": {
                "type": "string",
                "description": (
                    "What is concretely present in the final page state: the elements, their text, their "
                    "state. Describe what is there, not whether it is good enough."
                ),
            },
        },
        "required": ["observation"],
    },
}

_SYSTEM_PROMPT: Final[str] = (
    "You are verifying a web app that another AI agent built for a non-technical client. You drive a "
    "real Chromium browser one action at a time.\n\n"
    "You are given the flow's declared steps, the actions you have already taken -- each with what "
    "you expected of it and what the page actually did -- and the current page as its URL, its "
    "title and its accessibility tree. Choose the SINGLE next action.\n\n"
    "Rules:\n"
    "- Address an element by the ARIA role and accessible name the current page state gives it, spelled "
    "exactly as the tree spells them. Never invent a name the page does not show.\n"
    "- Do exactly what the steps say. Do not improve the app, work around bugs, or try alternative "
    "routes to make a broken app look like it works -- the point is to find out whether it works.\n"
    "- If the app is broken, unresponsive, or missing what the steps need, choose 'done' and say so in "
    "your reasoning rather than hunting for a workaround.\n"
    "- After typing into a field you usually need a separate action to submit it (press Enter, or "
    "click the button).\n"
    "- To EDIT text an element already shows (a heading, a label, a cell), use 'input' on that "
    "element even when its role is not 'textbox': many apps make text editable in place, and no "
    "input element ever appears. Commit with Enter. Success shows as the element's text having "
    "changed, not as an edit control appearing.\n"
    "- Never repeat an action the history says left the page unchanged. Clicking the same thing "
    "again will change nothing; take the next plausible gesture instead (for an edit, 'input' into "
    "the element, then Enter).\n"
    "- More generally, where the history shows an action whose effect did not match what you "
    "expected of it, take that as the page telling you how it really works, and act on the "
    "corrected understanding rather than repeating the action.\n"
    "- Choose 'done' as soon as every declared step has been carried out."
)

_READING_SYSTEM_PROMPT: Final[str] = (
    "You are recording evidence about a web app that another AI agent built for a non-technical "
    "client.\n\n"
    "A flow has just been driven through the app. Given the flow's steps and the final page state, "
    "describe what that state actually shows.\n\n"
    "Report only what is observable: which elements are present, what they say, what state they are "
    "in. Do not decide whether the flow succeeded, do not score the app, and do not speculate about "
    "what was intended -- something else rules on that, from this description and the screenshots."
)


@pure
def truncate_state(state_text: str) -> str:
    """The head AND the tail of a page state, with the middle elided. The head -- the URL, the title
    and the top of the accessibility tree -- is the part most actions are chosen from, but a panel
    or dialog the last action opened renders at the END of the tree, so a head-only cut would show
    the agent a page on which its click did nothing. The cuts land on line boundaries because the
    tree is line-oriented and a half line reads as an element that is not there."""
    if len(state_text) <= MAX_STATE_PROMPT_CHARS:
        return state_text
    head = state_text[: MAX_STATE_PROMPT_CHARS - TAIL_STATE_PROMPT_CHARS]
    if "\n" in head:
        head = head.rsplit("\n", 1)[0]
    tail = state_text[-TAIL_STATE_PROMPT_CHARS:]
    if "\n" in tail:
        tail = tail.split("\n", 1)[1]
    return "{}\n[...page state truncated; the end of the page follows...]\n{}".format(head, tail)


@pure
def fit_history(history: Sequence[str]) -> list[str]:
    """The history as the prompt carries it, newest first in importance and oldest first in order.

    Recent entries go in whole; older ones keep only their action line, since what a step did stays
    worth knowing long after the detail of how the page moved has stopped being actionable. Once
    even that does not fit, the rest are dropped and counted, so the prompt never claims a step did
    not happen just because there was no room to describe it.
    """
    kept: list[str] = []
    spent = 0
    for entry in reversed(history):
        for candidate in (entry, entry.splitlines()[0]):
            if spent + len(candidate) <= MAX_HISTORY_PROMPT_CHARS:
                kept.append(candidate)
                spent += len(candidate)
                break
        else:
            dropped = len(history) - len(kept)
            kept.append("({} earlier action{} not shown)".format(dropped, "" if dropped == 1 else "s"))
            break
    return list(reversed(kept))


@pure
def build_action_prompt(flow_steps: str, history: tuple[str, ...], state_text: str) -> str:
    """The next-action prompt: what the flow asks for, what has been done, and what the page shows."""
    history_prose = "\n".join("{}. {}".format(index + 1, entry) for index, entry in enumerate(fit_history(history)))
    return (
        "Flow steps to carry out:\n{steps}\n\nActions taken so far:\n{history}\n\nCurrent page state:\n{state}"
    ).format(
        steps=flow_steps,
        history=history_prose or "(none yet -- this is the first action)",
        state=truncate_state(state_text) or "(the browser reported no page state)",
    )


@pure
def build_reading_prompt(flow_steps: str, history: tuple[str, ...], state_text: str) -> str:
    """The closing prompt: what the flow asked for, what was done, and what the page ended up showing.

    The flow's `expect` is deliberately NOT included. Naming the condition invites the model to rule
    on it, and ruling on it is the grade-time judge's job.
    """
    history_prose = "\n".join("{}. {}".format(index + 1, entry) for index, entry in enumerate(fit_history(history)))
    return (
        "Flow steps that were carried out:\n{steps}\n\nActions actually taken:\n{history}\n\nFinal page state:\n{state}"
    ).format(
        steps=flow_steps,
        history=history_prose or "(no actions were taken)",
        state=truncate_state(state_text) or "(the browser reported no page state)",
    )


@pure
def describe_step(described_action: str, expected: str, error: str, observed: str) -> str:
    """One history entry: what was done, what it was meant to achieve, and what the page did.

    All three in one entry, indented under the action, because the entries are numbered in the
    prompt and a change spanning several lines would otherwise break the numbering apart. The
    prediction sits beside the observation so the latter reads as confirming or contradicting
    something rather than as an isolated fact about the page.
    """
    lines = [described_action]
    if expected:
        lines.append("   expected: {}".format(expected))
    if error:
        lines.append("   did not run: {}".format(error))
    for index, line in enumerate(observed.splitlines()):
        lines.append("   {} {}".format("the page then:" if index == 0 else "              ", line))
    return "\n".join(lines)


@pure
def describe_action(action: FlowAction) -> str:
    """A one-line record of an action, for the flow log and the next prompt's history."""
    match action.kind:
        case FlowActionKind.CLICK:
            return "click the {} named {!r}".format(action.role, action.target)
        case FlowActionKind.INPUT:
            return "type {!r} into the {} named {!r}".format(action.text, action.role, action.target)
        case FlowActionKind.KEYS:
            return "press {}".format(action.text)
        case FlowActionKind.SCROLL:
            return "scroll by {}px".format(action.amount)
        case FlowActionKind.OPEN:
            return "open {}".format(action.text)
        case FlowActionKind.RELOAD:
            return "reload the page"
        case FlowActionKind.DONE:
            return "finish the flow"
        case _ as unreachable:
            assert_never(unreachable)


# Actions that address an element, and so are meaningless without a name to address it by.
_TARGETED_ACTION_KINDS: Final[frozenset[FlowActionKind]] = frozenset({FlowActionKind.CLICK, FlowActionKind.INPUT})


@pure
def parse_action(tool_input: dict[str, Any]) -> FlowAction | None:
    """One tool-call payload into an action.

    None when the payload does not describe an action that can be performed -- an action name that
    does not exist, or one that addresses an element without naming it. Both are recorded as a call
    that produced nothing rather than coerced into something the model did not ask for: an unnamed
    target would otherwise resolve to whatever the page happens to list first.
    """
    raw_kind = str(tool_input.get("action") or "").strip().lower()
    if raw_kind not in {member.value for member in FlowActionKind}:
        return None
    kind = FlowActionKind(raw_kind)
    target = str(tool_input.get("target") or "").strip()
    if kind in _TARGETED_ACTION_KINDS and not target:
        return None
    raw_amount = tool_input.get("amount")
    return FlowAction(
        kind=kind,
        role=str(tool_input.get("role") or "").strip(),
        target=target,
        text=str(tool_input.get("text") or ""),
        amount=raw_amount if isinstance(raw_amount, int) and not isinstance(raw_amount, bool) else 0,
        reasoning=str(tool_input.get("reasoning") or "").strip(),
        expected=str(tool_input.get("expected") or "").strip(),
    )


class VerifierCall(FrozenModel):
    """One verification-agent model call: the parsed tool payload plus its usage."""

    tool_input: dict[str, Any] | None = Field(description="The tool payload, or None when the call yielded none")
    input_token_count: int = Field(description="Input tokens the call consumed")
    output_token_count: int = Field(description="Output tokens the call consumed")


def _call_tool(
    system_prompt: str, prompt: str, tool: ToolParam, model: str, api_key: str, timeout_seconds: float
) -> VerifierCall:
    """One forced-tool call, with its usage kept for the harness spend account. A call that comes
    back with no payload ends the flow with a recorded instrument failure rather than raising, so a
    flaky API never takes the whole evidence phase down."""
    call = model_calls.call_forced_tool(
        system_prompt=system_prompt,
        prompt=prompt,
        tools=[tool],
        model=model,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        max_tokens=MAX_TOKENS,
        caller_label="verification agent's {}".format(tool["name"]),
    )
    return VerifierCall(
        tool_input=call.tool_input,
        input_token_count=call.input_token_count,
        output_token_count=call.output_token_count,
    )


class VerificationAgent(MutableModel, ABC):
    """Decides one flow's next browser action, and describes the state it ended in.

    An interface rather than two module functions so the evidence collector can be exercised
    against a scripted agent, the way the driver's turn loop is exercised against a scripted
    TurnSource. Every call's usage is returned alongside the answer, so nothing has to reach back
    into the agent to account for what a flow cost.
    """

    calls: list[VerifierCall] = Field(default_factory=list, description="Every call made, in order")

    @abstractmethod
    def decide_next_action(
        self, flow_steps: str, history: tuple[str, ...], state_text: str
    ) -> tuple[FlowAction | None, VerifierCall]:
        """The single next browser action, or None when the call produced nothing usable."""

    @abstractmethod
    def read_final_state(
        self, flow_steps: str, history: tuple[str, ...], state_text: str
    ) -> tuple[FlowReading | None, VerifierCall]:
        """What the final page state shows, or None when the call produced nothing usable."""


class AnthropicVerificationAgent(VerificationAgent):
    """The real agent: two forced-tool calls against the Anthropic API, same plumbing as the decider."""

    model: str = Field(frozen=True, description="The model to reason with")
    api_key: SecretStr = Field(frozen=True, description="Anthropic API key")
    timeout_seconds: float = Field(frozen=True, description="Per-call HTTP timeout")

    def _call(self, system_prompt: str, prompt: str, tool: ToolParam) -> VerifierCall:
        call = _call_tool(
            system_prompt, prompt, tool, self.model, self.api_key.get_secret_value(), self.timeout_seconds
        )
        self.calls.append(call)
        return call

    def decide_next_action(
        self, flow_steps: str, history: tuple[str, ...], state_text: str
    ) -> tuple[FlowAction | None, VerifierCall]:
        call = self._call(_SYSTEM_PROMPT, build_action_prompt(flow_steps, history, state_text), _ACTION_TOOL)
        if call.tool_input is None:
            return None, call
        return parse_action(call.tool_input), call

    def read_final_state(
        self, flow_steps: str, history: tuple[str, ...], state_text: str
    ) -> tuple[FlowReading | None, VerifierCall]:
        call = self._call(_READING_SYSTEM_PROMPT, build_reading_prompt(flow_steps, history, state_text), _READING_TOOL)
        observation = str((call.tool_input or {}).get("observation") or "").strip()
        if not observation:
            return None, call
        return FlowReading(observation=observation), call


@pure
def summarize_verifier_usage(calls: tuple[VerifierCall, ...], model: str) -> VerifierUsage:
    return VerifierUsage(
        model=model,
        call_count=len(calls),
        failed_call_count=sum(1 for call in calls if call.tool_input is None),
        input_token_count=sum(call.input_token_count for call in calls),
        output_token_count=sum(call.output_token_count for call in calls),
    )


# --- the box-side executor ---

# Where the step script and the browser live in the box, and how they find each other.
BOX_FLOW_STEP_PATH: Final[str] = "/tmp/box_flow_step.py"
# Where the first flow's browser listens for CDP; each later flow takes the next port up. Clear of
# the desktop stack's 5900/6080 and of the Minds backend's discovered port.
CDP_BASE_PORT: Final[int] = 9333

# Where the box image installs Playwright's browsers. The image sets this as an ENV of its own (a
# test pins the two together); the launch command passes it again so which browser gets resolved
# never depends on what an exec happens to inherit.
BOX_PLAYWRIGHT_BROWSERS_PATH: Final[str] = "/opt/ms-playwright"
# What the resolution that found the browser wrote, for the trace to quote after a failure.
BROWSER_RESOLVE_LOG_PATH: Final[str] = "/tmp/chromium_resolve.log"

# Run in the box's own venv -- the one that installed the browser -- to print the path of the
# Chromium playwright holds. One expression, so the last stdout line is the path and nothing else.
_CHROMIUM_PATH_SNIPPET: Final[str] = (
    "from playwright.sync_api import sync_playwright\n"
    "with sync_playwright() as playwright:\n"
    "    print(playwright.chromium.executable_path)\n"
)

# One browser per flow, launched before its first step and connected to per step, so page and
# session state persist without this side holding a long-lived protocol of its own. Per FLOW rather
# than per phase because the state that persists has to stop somewhere: a browser of its own gives
# a flow a fresh profile -- cookies, storage, cache -- that no earlier flow can have touched.
BROWSER_READY_ATTEMPT_COUNT: Final[int] = 20
BROWSER_READY_POLL_SECONDS: Final[float] = 1.5


# The throwaway Chromium profiles, one per flow. A profile is the browser's whole memory -- cookies,
# storage, cache -- so a fresh one per flow is what makes flows independent of each other.
BOX_CHROMIUM_PROFILE_PREFIX: Final[str] = "/tmp/minds-evals-chromium-"


@pure
def flow_browser_port(flow_index: int) -> int:
    """The CDP port the flow at this index drives."""
    return CDP_BASE_PORT + flow_index


@pure
def flow_profile_dir(flow_index: int) -> str:
    """The profile directory the flow at this index drives its browser on."""
    return "{}{}".format(BOX_CHROMIUM_PROFILE_PREFIX, flow_index)


@pure
def cdp_endpoint(port: int) -> str:
    return "http://127.0.0.1:{}".format(port)


# How much of a failed step's error text the driver keeps. The step script bounds its own detail to
# the same size; this catches what arrives from a step that never got to bound anything.
MAX_STEP_DETAIL_CHARS: Final[int] = 2000

# Budgets. A flow is a handful of interactions; anything longer is looping, not progressing. This is
# also the model-call cap, and the only one needed: a flow makes exactly one call per step plus one
# for the closing reading, so it can never exceed MAX_STEPS_PER_FLOW + 1.
MAX_STEPS_PER_FLOW: Final[int] = 15

# Reasons recorded on a flow entry the harness could not measure. Each names a distinct layer,
# because "the executor broke" and "the agent builds bad apps" must never read alike.
REASON_BROWSER_LAUNCH_FAILED: Final[str] = "browser_launch_failed"
REASON_VERIFIER_AGENT_FAILED: Final[str] = "verifier_agent_failed"
REASON_STEP_BRIDGE_FAILED: Final[str] = "step_bridge_failed"

# The half of the vocabulary the step script writes, restated here so a reader of this module sees
# the whole taxonomy in one place. The VALUES come from the protocol both sides validate against,
# so restating them cannot make the two disagree.
REASON_CDP_CONNECT_FAILED: Final[str] = flow_step_protocol.REASON_CDP_CONNECT_FAILED
REASON_FORWARD_UNREACHABLE: Final[str] = flow_step_protocol.REASON_FORWARD_UNREACHABLE
REASON_TUNNEL_DOWN: Final[str] = flow_step_protocol.REASON_TUNNEL_DOWN
REASON_TLS_REFUSED: Final[str] = flow_step_protocol.REASON_TLS_REFUSED
REASON_UNKNOWN_ACTION: Final[str] = flow_step_protocol.REASON_UNKNOWN_ACTION
REASON_STEP_ERROR: Final[str] = flow_step_protocol.REASON_STEP_ERROR
# The page did not offer what the action asked for in the time allowed. The browser is fine, so
# this is the app falling short rather than the instrument.
REASON_ACTION_TIMED_OUT: Final[str] = flow_step_protocol.REASON_ACTION_TIMED_OUT
# The workspace's agent id is not a coordinate the proxy routes on, so no forwarded origin can be
# built. The workspace may be serving perfectly; this is the harness holding an identity it cannot
# address, so it must not be charged to the agent the way an empty registry is.
REASON_WORKSPACE_UNADDRESSABLE: Final[str] = "workspace_unaddressable"

# Reasons recorded on a flow the WORKSPACE fell short of.
REASON_NO_APP_TO_OPEN: Final[str] = "no_app_to_open"
REASON_STEP_BUDGET_EXHAUSTED: Final[str] = "step_budget_exhausted"
REASON_FLOW_DEADLINE: Final[str] = "flow_deadline"

# The executor-level reasons: a browser that cannot be driven at all, so the flow stops.
_INSTRUMENT_REASONS: Final[frozenset[str]] = frozenset(
    {
        REASON_BROWSER_LAUNCH_FAILED,
        REASON_CDP_CONNECT_FAILED,
        REASON_FORWARD_UNREACHABLE,
        REASON_TUNNEL_DOWN,
        REASON_TLS_REFUSED,
        REASON_STEP_BRIDGE_FAILED,
        REASON_UNKNOWN_ACTION,
        REASON_STEP_ERROR,
    }
)


class StepOutcome(FrozenModel):
    """What one step of a flow did, as reported by the box-side step script."""

    is_ok: bool = Field(description="Whether the action landed and the page was readable")
    reason: str = Field(description="Which layer failed, empty when the step succeeded")
    detail: str = Field(description="Bounded error text from the executor")
    state_text: str = Field(description="The page after the action: URL, title and its ARIA tree")
    screenshot_name: str = Field(description="The frame captured after the action, empty when none was")


@pure
def is_instrument_reason(reason: str) -> bool:
    """Whether a reason means the browser cannot be driven further.

    The distinction the whole taxonomy rests on: an instrument reason ends the flow as ERROR and is
    excluded from scoring, while an action that simply did not work leaves the browser usable, so
    the flow records it and carries on within its step budget.
    """
    return reason in _INSTRUMENT_REASONS


@pure
def parse_step_result(stdout: str) -> StepOutcome:
    """One step script's reply.

    Anything that does not validate as a StepResult means the script never got to speak for itself
    -- the upload is missing, python is broken, the exec died -- which is the bridge failing rather
    than anything about the app. The JSON is found by its first brace because `uv run` may print
    lines of its own above it.
    """
    stripped = stdout.strip()
    start = stripped.find("{")
    if start == -1:
        return _bridge_failure(stripped)
    try:
        result = StepResult.model_validate_json(stripped[start:])
    except ValidationError:
        return _bridge_failure(stripped)
    return StepOutcome(
        is_ok=result.is_ok,
        reason=result.reason,
        detail=result.detail,
        state_text=render_page_state(result.url, result.title, result.snapshot),
        screenshot_name=result.screenshot_path.rsplit("/", 1)[-1],
    )


@pure
def _bridge_failure(stdout: str) -> StepOutcome:
    """What a step whose reply never arrived reports: the raw output, bounded, and no page."""
    return StepOutcome(
        is_ok=False,
        reason=REASON_STEP_BRIDGE_FAILED,
        detail=stdout[:MAX_STEP_DETAIL_CHARS],
        state_text="",
        screenshot_name="",
    )


@pure
def render_page_state(url: str, title: str, snapshot: str) -> str:
    """The page as the verification agent reads it, and as the flow log records it verbatim.

    URL and title lead because a flow's whole point can turn on them -- a reload that lost the
    session lands somewhere else entirely -- and the ARIA tree follows as the addressable content.
    """
    if not url and not snapshot:
        return ""
    return "page {} ({})\n{}".format(url, title, snapshot)


@pure
def build_step_request(
    action: FlowAction | None,
    screenshot_path: str,
    cdp_endpoint_url: str,
    preauth_cookie: str,
    cookie_domain: str,
) -> str:
    """The JSON one step script invocation receives.

    A cookie rides the FIRST request of a flow, so its opening navigation is already authenticated;
    later steps land in the same browser, which is still holding the session. The scope it is
    installed at is `forward_instance.session_cookie_domain`.
    """
    cookie = (
        StepCookie(name=SESSION_COOKIE_NAME, value=preauth_cookie, domain=cookie_domain) if preauth_cookie else None
    )
    return StepRequest(
        cdp_endpoint=cdp_endpoint_url,
        screenshot_path=screenshot_path,
        action=_step_action(action),
        cookie=cookie,
    ).model_dump_json()


@pure
def _step_action(action: FlowAction | None) -> StepAction:
    """One decided action in the step script's vocabulary. None performs nothing and just reads the
    page, which is how a flow gets its first look before deciding anything."""
    if action is None:
        return StepAction(kind=StepActionKind.NOOP)
    return StepAction(
        kind=StepActionKind(action.kind.value),
        role=action.role,
        target=action.target,
        text=action.text,
        amount=action.amount,
    )


@pure
def chromium_path_command() -> str:
    """Ask the box's own Playwright where its Chromium executable is.

    Nothing here matches a path. The layout under the browsers root is Playwright's private
    business and it moves: the directory carries the browser revision, the one below it names the
    platform (`chrome-linux64` on linux-x64, `chrome-linux` on linux-arm64, an .app bundle on
    macOS), and a headless-shell build sits in a sibling tree. Asking the installed package is the
    only resolution that survives a version bump, and it fails where it is read when the install is
    missing.

    `chromium.executable_path` names the FULL Chrome build, which is what `--headless=new` needs --
    the headless shell beside it is a separate executable that does not take that flag.
    """
    return "cd {mngr} && PLAYWRIGHT_BROWSERS_PATH={root} uv run python -c {snippet}".format(
        mngr=minds_bridge.BOX_MNGR_DIR,
        root=shlex.quote(BOX_PLAYWRIGHT_BROWSERS_PATH),
        snippet=shlex.quote(_CHROMIUM_PATH_SNIPPET),
    )


@pure
def browser_launch_command(flow_index: int) -> str:
    """Launch the headless Chromium one flow connects to, on its own profile and CDP port.

    Every browser an earlier flow left behind is killed first, and this flow's profile is recreated
    empty: only one flow runs at a time, so an earlier browser is nothing but held memory, and a
    surviving profile would be a channel between flows.

    The sweep is written so it cannot match the command line running it -- `pkill -f` reads every
    process's argv, this one included, and matching itself would take the shell down before the
    browser ever started. Two things keep that from happening: the pattern's leading `[-]-`, and
    passing the profile to Chromium through a variable, so the literal `--user-data-dir=<path>` the
    pattern looks for appears only in the browser's own argv.

    Backgrounded with setsid, because `environment.exec` returns as soon as its command does and
    the browser has to outlive it. The resolved path is echoed so the trace records which binary
    actually ran.

    --no-sandbox because the box runs as root, which is where Chromium refuses to start its
    sandbox; --disable-dev-shm-usage because a container's default /dev/shm is too small for
    Chromium's renderer and it crashes in ways that read as a broken app.
    """
    return (
        'profile={profile}; pkill -f {stale} || true; rm -rf "$profile"; mkdir -p "$profile"; '
        "chrome=$({resolve} 2>{resolve_log} | tail -n 1); "
        'if [ ! -x "$chrome" ]; then echo "playwright resolved no runnable chromium: ${{chrome:-(none)}}"; '
        "cat {resolve_log} 2>/dev/null; exit 97; fi; "
        # A tripwire, not a fallback: resolution is not supposed to be able to return the headless
        # shell, and a shell launched with --headless=new dies on the flag rather than serving CDP.
        'case "$chrome" in *headless*) '
        'echo "playwright resolved the headless shell, which --headless=new cannot run: $chrome"; exit 97;; esac; '
        'setsid nohup "$chrome" --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage '
        "--remote-debugging-address=127.0.0.1 --remote-debugging-port={port} "
        '--user-data-dir="$profile" --ignore-certificate-errors '
        "> {launch_log} 2>&1 < /dev/null & "
        'echo "launched $chrome"'
    ).format(
        profile=shlex.quote(flow_profile_dir(flow_index)),
        stale=shlex.quote("[-]-user-data-dir={}".format(BOX_CHROMIUM_PROFILE_PREFIX)),
        resolve=chromium_path_command(),
        resolve_log=BROWSER_RESOLVE_LOG_PATH,
        port=flow_browser_port(flow_index),
        launch_log=browser_launch_log_path(flow_browser_port(flow_index)),
    )


@pure
def browser_probe_command(port: int) -> str:
    """Whether the browser is accepting CDP yet. Chromium binds its debug port a moment after the
    process starts, so this is polled rather than assumed."""
    return "curl -s --max-time 5 {}/json/version".format(cdp_endpoint(port))


@pure
def browser_launch_log_path(port: int) -> str:
    """One log per browser: each flow's browser writes its own, so a launch failure is read against
    the flow that hit it."""
    return "/tmp/chromium_launch_{}.log".format(port)


@pure
def step_command(step_request: str) -> str:
    """Run one step in the box, against the venv the box already syncs (which is where the pinned
    playwright lives)."""
    return "cd {mngr} && uv run python {script} {request}".format(
        mngr=minds_bridge.BOX_MNGR_DIR, script=BOX_FLOW_STEP_PATH, request=shlex.quote(step_request)
    )


# What a state summary says when the two states are identical. Kept as one fixed sentence because
# it is the signal an agent loops hardest without: an action that landed and changed nothing
# (observed: 15 identical clicks on an in-place-editable heading whose only click feedback was a
# CSS focus wash).
UNCHANGED_STATE_SUMMARY: Final[str] = "the page state is exactly the same as before that action"


# What a browser rewrites on every render rather than because the page changed: element ids are
# renumbered whenever the tree is rebuilt, and focus and cursor follow the pointer. Comparing them
# makes an untouched page look rewritten -- on a live run one step reported 27 changed lines in a
# 24-line tree, of which 18 were nothing but these.
_VOLATILE_ATTRIBUTES: Final[re.Pattern[str]] = re.compile(r"\s*\[(?:ref=e\d+|active|cursor=[^\]]*)\]")


@pure
def comparable_line(line: str) -> str:
    """A page line reduced to the content a reader would say it holds.

    Both compared and displayed in this form: an id the next render will renumber is not something
    the agent can act on, and it crowds out the part of the line that is.
    """
    return _VOLATILE_ATTRIBUTES.sub("", line).strip()


@pure
def summarize_state_change(previous_state: str, current_state: str) -> str:
    """How the page moved, in the few lines the next decision needs to check its prediction against.

    The accessibility tree is line-oriented, so a line is the unit: lines the action added and lines
    it removed, named up to `MAX_SUMMARY_LINES` and bounded per line. Past `MAX_SUMMARY_CHANGE_RATIO`
    of the tree the page has effectively been replaced, and listing lines describes nothing, so the
    summary says only how much moved.

    Deliberately a summary and not a diff: the whole current state is already in the prompt below
    it, so this exists to point at the delta, not to re-transmit the page.
    """
    previous_lines = [comparable_line(line) for line in previous_state.splitlines()]
    current_lines = [comparable_line(line) for line in current_state.splitlines()]
    if previous_lines == current_lines:
        return UNCHANGED_STATE_SUMMARY
    previous_counts = Counter(previous_lines)
    current_counts = Counter(current_lines)
    removed = _surplus_lines(previous_lines, previous_counts - current_counts)
    added = _surplus_lines(current_lines, current_counts - previous_counts)
    if not removed and not added:
        # Every line of one state is matched by a line of the other, as many times over -- the only
        # thing that can differ is the order they come in.
        return "the page has the same elements in a different arrangement"
    largest = max(len(previous_lines), len(current_lines), 1)
    changed = len(removed) + len(added)
    if changed > MIN_REPLACEMENT_LINES and changed / largest > MAX_SUMMARY_CHANGE_RATIO:
        return "most of the page changed: {} lines gone, {} new, of {}".format(len(removed), len(added), largest)
    return "\n".join(_summary_lines(added, "new:") + _summary_lines(removed, "gone:"))


@pure
def _surplus_lines(lines: Sequence[str], surplus: Counter[str]) -> list[str]:
    """The lines of one state that the other state does not account for, in the order they appear.

    Counted rather than merely tested for presence: a page that drops one of several identical rows
    -- a list with two copies of the same item, one of them just deleted -- still shows that line, so
    a membership test would report the deletion as no change at all.

    Blank lines are left out here rather than at the point of display, so that the lines named and
    the count of the ones that were not come from the same list.
    """
    remaining = surplus.copy()
    kept: list[str] = []
    for line in lines:
        if remaining[line] > 0:
            remaining[line] -= 1
            if line:
                kept.append(line)
    return kept


@pure
def _summary_lines(lines: Sequence[str], marker: str) -> list[str]:
    """Up to `MAX_SUMMARY_LINES` of one side of a change, each bounded, with a count of the rest.

    The marker is a word rather than a sign because every line of an accessibility tree already
    begins with "- ": a "-" marker would render a removed row as "- - row M-101", which reads as
    two levels of nesting rather than as a deletion.
    """
    if not lines:
        return []
    shown = ["{} {}".format(marker, _bounded_line(line)) for line in lines[:MAX_SUMMARY_LINES]]
    if len(lines) > MAX_SUMMARY_LINES:
        shown.append("{} and {} more".format(marker, len(lines) - MAX_SUMMARY_LINES))
    return shown


@pure
def _bounded_line(line: str) -> str:
    return line if len(line) <= MAX_SUMMARY_LINE_CHARS else line[:MAX_SUMMARY_LINE_CHARS] + "..."


@pure
def flow_init_record(goal: str, expect: str, url: str, state_text: str, screenshot_name: str, timestamp: str) -> str:
    """The first line of a flow's log: what the flow was asked to do, and the page it opened onto.

    The opening navigation is already made and already screenshotted before any action is decided,
    so this records what was there rather than capturing anything new. Without it a reader meets the
    flow one action in, looking at the frame that followed the first action and with nothing saying
    what the agent was trying to achieve.

    `goal` and `expect` are the case's own declarations, copied here so the log stands on its own.
    The grade-time judge reads them from the case instead, and rules on the `expect` from the
    evidence below; nothing here decides anything.
    """
    return json.dumps(
        {
            "kind": FlowRecordKind.INIT.value,
            "step_index": 0,
            "timestamp": timestamp,
            "goal": goal,
            "expect": expect,
            "url": url,
            "state": state_text,
            "screenshot": screenshot_name,
            "error": "",
        }
    )


@pure
def flow_final_record(step_index: int, observation: str, state_text: str, timestamp: str) -> str:
    """The last line of a flow's log: what the agent says the final page showed.

    Labelled a reading rather than a verdict. It is context for the grade-time judge, which is what
    actually rules on the flow's `expect`.

    `action` and `reasoning` carry the reading as well as the structured fields, because the judge's
    digest finds this record by its action text and prints its reasoning.
    """
    return json.dumps(
        {
            "kind": FlowRecordKind.FINAL.value,
            "step_index": step_index,
            "timestamp": timestamp,
            "action": "read the final state",
            "reasoning": observation or "(no reading recorded)",
            "observation": observation,
            "state": state_text,
            "screenshot": "",
            "error": "",
        }
    )


@pure
def flow_step_record(
    step_index: int,
    action: str,
    reasoning: str,
    expected: str,
    observed: str,
    state_text: str,
    screenshot_name: str,
    error: str,
    timestamp: str,
) -> str:
    """One line of a flow's log.jsonl: the verbatim page state the agent saw, what it decided, why,
    and whether the browser actually carried it out.

    The state is the page BEFORE the action and the screenshot is the page AFTER it, so a reader
    walking the log sees cause and effect in each record. A finishing step names no screenshot: it
    performs nothing, so there is no resulting frame.

    ``error`` is what makes the log honest. The grade-time judge rules on the `expect` from this
    evidence, so a step that shows "click the button named 'Delete'" followed by an unchanged
    screenshot -- with nothing saying the click never landed -- would actively mislead it.

    The state is recorded UNTRUNCATED: the judge reads this file, and it is the cheap, token-dense
    alternative to looking at screenshots.

    """
    return json.dumps(
        {
            "kind": FlowRecordKind.ACTION.value,
            "step_index": step_index,
            "timestamp": timestamp,
            "action": action,
            "reasoning": reasoning,
            "expected": expected,
            "observed": observed,
            "state": state_text,
            "screenshot": screenshot_name,
            "error": error,
        }
    )
