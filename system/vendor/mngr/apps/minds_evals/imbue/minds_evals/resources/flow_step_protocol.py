"""The contract between the driver and the box-side step script: one step's request and its reply.

Both sides import this module -- the driver as `imbue.minds_evals.resources.flow_step_protocol`,
the step script as a plain module beside it, since both are uploaded into the same directory in the
box. It therefore imports nothing but pydantic and imbue_common, which is all the box's venv is
guaranteed to hold for code this project ships but does not resolve dependencies for.

Typed on both sides so a malformed payload fails at the boundary, naming the field that broke,
rather than surfacing deep inside the step as a missing key or a silently defaulted value. The
reason vocabulary lives here for the same reason: the script writes it and the driver classifies on
it, and the two must not be able to drift.
"""

import re
from enum import auto
from typing import Self

from pydantic import Field
from pydantic import ValidationError
from pydantic import model_validator

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel

# The layer a step implicates when it fails. The instrument's own failures are separated from the
# app's, because "the executor broke" and "the agent builds bad apps" must never read alike.
REASON_CDP_CONNECT_FAILED = "cdp_connect_failed"
REASON_FORWARD_UNREACHABLE = "forward_unreachable"
REASON_TLS_REFUSED = "tls_refused"
REASON_TUNNEL_DOWN = "tunnel_down"
# The page did not offer what the flow asked for in the time allowed -- a locator that never
# resolved, a navigation that never settled. The browser is fine, so this one is the app's.
REASON_ACTION_TIMED_OUT = "action_timed_out"
# A ref the agent read off one snapshot no longer names the same thing on a fresh one: the element
# is gone, or the page changed and the number sits on another role. The browser and the page are
# fine; the agent has to address the element again from the state it is now shown.
REASON_STALE_REF = "stale_ref"
# An action kind the script cannot perform. The driver decides the vocabulary, so this is the
# harness contradicting itself and nothing to do with the app.
REASON_UNKNOWN_ACTION = "unknown_action"
# Anything else that came out of the executor: a closed target, a dropped browser, a protocol
# error, a bug in the script. Unknown, but unknown on THIS side of the glass.
REASON_STEP_ERROR = "step_error"


class StepActionKind(LowerCaseStrEnum):
    """What one step does to the page.

    NOOP performs nothing and lets the capture report the page as it stands, which is how a caller
    takes a look without acting. WAIT also performs nothing, but gives the page time to change
    before the capture: it is how a flow lets a pending state (a spinner, a "saving" notice) run its
    course without reloading. RELOAD is its own kind rather than a re-OPEN: a reload keeps the URL
    and the session, which is exactly what a persistence flow is testing.
    """

    NOOP = auto()
    OPEN = auto()
    RELOAD = auto()
    CLICK = auto()
    INPUT = auto()
    KEYS = auto()
    SCROLL = auto()
    WAIT = auto()


class StepReaction(LowerCaseStrEnum):
    """What the page's DOM did in the moments after an action, as the step script watched it.

    Watched through a MutationObserver, so this is about the DOM and not the accessible tree: an
    action the page answered with nothing but a CSS class still counts as a reaction here, while
    the tree the driver diffs is unchanged. The two together are what let the driver tell "the
    control is dead" from "the control acknowledged the click without showing anything new".

    The observer watches the main document's tree, so NONE is that tree going untouched rather than
    the app doing nothing: a reaction confined to a shadow root, to a subframe, or to pixels drawn
    into a canvas is outside what it can see and reads as NONE, and so does a gesture that changes
    only the viewport, since scrolling mutates nothing. That bound is worth knowing, because NONE is
    the verdict the driver turns into an instruction not to repeat the action.

    UNOBSERVED is what a step reports when it did not watch at all: OPEN and RELOAD wait for the
    network instead, a bare read performs nothing to react to, and an action that failed never got
    as far as watching. A CLICK that turns out to navigate is watched like any other click, but the
    watch dies with the document it was installed on, so what it reports is SETTLED: the new
    document arrived and its network went quiet. Nothing observes that document's own DOM, and the
    capture beside the reaction is the page the click led to.
    """

    UNOBSERVED = auto()
    # No DOM mutation arrived before the cap ran out. The positive "nothing happened" signal.
    NONE = auto()
    # Mutations arrived and then stopped: the page reacted and reached a stable state.
    SETTLED = auto()
    # Mutations were still arriving when the cap ran out: a timer, a poll, an animation.
    STILL_CHANGING = auto()


# How long a step waits for the page to START reacting, by how entitled it is to a reaction. A click
# or a key press is a gesture the app is expected to answer, so its cap covers the slow renders a
# real app has (measured up to 1.5s on batched frameworks) and its expiry means the control is
# dead. Typing and scrolling oblige the app to nothing -- typing's own effect is the field's text,
# which the tree shows, and a scroll's is the viewport, which neither the tree nor the DOM shows --
# so a reaction to either is a bonus (live validation, lazy loading) and is not waited for long.
# A WAIT action is the flow explicitly giving a pending state time, so it waits longest.
EXPECTED_REACTION_CAP_MS = 3_000
POSSIBLE_REACTION_CAP_MS = 1_000
WAIT_ACTION_CAP_MS = 10_000
# How long the DOM has to stay untouched, once it has started changing, to count as settled.
# Comfortably longer than a frame and shorter than any perceptible pause. A first reaction that
# arrives, pauses this long, and is followed by more (a two-phase render) is read at its first
# stable point, which is the state a user would have seen too.
QUIET_MS = 250
# How long a page that has started changing but never goes quiet is given up on, counted from its
# first mutation or from the end of the action, whichever is later. The observer is installed before
# the action, so on a page with a timer of its own the first mutation can predate the action itself;
# counting from it alone would let a slow action spend this whole budget before its own effect had
# any chance to land. A tight cap, because a page with a ticker pays all of it on every step.
SETTLE_CAP_MS = 5_000


# The shape of a snapshot ref: what Playwright's "ai" rendering prints as `[ref=e9]`.
REF_PATTERN = re.compile(r"^e\d+$")


class StepActionError(ValueError):
    """An action whose own fields contradict each other, so no page could satisfy it.

    A ValueError, because that is what pydantic folds into the ValidationError both sides read the
    boundary's verdict off.
    """


class StepAction(FrozenModel):
    """One decided browser action.

    Elements are addressed by ARIA role and accessible name, which is what the page snapshot is
    expressed in and what survives the page changing underneath the agent. An element the snapshot
    lists with no name is addressed by the ``ref`` the snapshot printed for it instead; the step
    resolves that against a fresh snapshot of the same page, so it holds only while the page has
    not changed since the snapshot it was read from.
    """

    kind: StepActionKind = Field(description="Which browser operation to perform")
    role: str = Field(default="", description="The target element's ARIA role, e.g. 'button'")
    target: str = Field(default="", description="The target element's accessible name")
    ref: str = Field(default="", description="The target element's snapshot ref, e.g. 'e9', when it has no name")
    text: str = Field(default="", description="Text to type, keys to press, or the URL to open")
    amount: int = Field(default=0, description="Scroll distance in pixels; negative scrolls up")

    @model_validator(mode="after")
    def _validate_ref_is_checkable(self) -> Self:
        if self.ref and not REF_PATTERN.match(self.ref):
            raise StepActionError("a ref is what the snapshot printed, such as 'e9', and nothing else")
        if self.ref and not self.role:
            raise StepActionError("a ref needs the role it was read on, which is what makes it checkable")
        return self


class StepCookie(FrozenModel):
    """The pre-arm cookie a flow's first step installs before its opening navigation.

    Scoped by domain rather than by the one origin the flow opens, because that is how the forward
    proxy issues the session it gates on (see `forward_instance.session_cookie_domain`).

    The fields carry every attribute the proxy sets except `Partitioned`, which is deliberately
    absent: it keys the jar by the embedding top-level site, and a flow drives the app top-level
    rather than inside a frame, so sending it would only risk the cookie being dropped.

    Field names are this project's; the step script translates them into the shape Playwright's
    `add_cookies` wants, so the third-party spelling stays at the one call site that needs it.
    """

    name: str = Field(description="The cookie the forward proxy gates its session on")
    value: str = Field(description="The token minted for this trial")
    # Refused empty here rather than in the box: Playwright rejects a cookie with neither a URL nor
    # a domain, and there that would be recorded against the flow instead of as the harness bug it is.
    domain: str = Field(min_length=1, description="The domain the cookie covers, leading dot included")
    path: str = Field(default="/", description="The path prefix the cookie is sent for")
    is_http_only: bool = Field(default=True, description="Whether script on the page may read it")
    is_secure: bool = Field(default=True, description="Whether it rides only HTTPS")
    same_site: str = Field(default="None", description="Playwright's SameSite value: Strict, Lax or None")


class StepRequest(FrozenModel):
    """Everything one invocation of the step script is told."""

    cdp_endpoint: str = Field(description="Where this flow's browser listens for CDP")
    screenshot_path: str = Field(description="Where to write the frame captured after the action")
    action: StepAction = Field(description="What to do before capturing the page")
    cookie: StepCookie | None = Field(default=None, description="Installed first, on a flow's opening step only")


class StepResult(FrozenModel):
    """Everything one invocation reports back, success or failure.

    A failure still carries the page: what the app showed when the action did not land is the most
    useful thing a flow can record, and the grade-time judge reads it.
    """

    is_ok: bool = Field(description="Whether the action landed and the page was readable")
    reason: str = Field(default="", description="Which layer failed, empty when the step succeeded")
    detail: str = Field(default="", description="Bounded error text")
    url: str = Field(default="", description="The page's URL after the action")
    title: str = Field(default="", description="The page's title after the action")
    snapshot: str = Field(default="", description="The page's ARIA tree after the action")
    screenshot_path: str = Field(default="", description="The frame written, empty when the capture failed")
    reaction: StepReaction = Field(
        default=StepReaction.UNOBSERVED, description="What the DOM did after the action, where the step watched"
    )


# One line of that rendering: an indented dash, an optional quote (Playwright quotes a whole line
# whose name holds characters that would break its own format), then the role.
_SNAPSHOT_LINE_ROLE_PATTERN = re.compile(r"^\s*-\s+'?([a-z]+)")


def snapshot_line_for_ref(snapshot: str, ref: str) -> str:
    """The snapshot line carrying `[ref=<ref>]`, or empty when no line does."""
    marker = "[ref={}]".format(ref)
    for line in snapshot.splitlines():
        if marker in line:
            return line
    return ""


def snapshot_line_role(line: str) -> str:
    """The ARIA role a snapshot line opens with, or empty for a line that is not an element."""
    match = _SNAPSHOT_LINE_ROLE_PATTERN.match(line)
    return match.group(1) if match else ""


# Where a validation error about an action kind points.
_ACTION_KIND_LOCATION = ("action", "kind")


def request_error_reason(error: ValidationError) -> str:
    """Which layer a request the step script cannot read implicates.

    An action kind with no member is the driver's vocabulary having outrun the script's, which is
    the same fault `unknown_action` names once a kind gets as far as being performed. Anything else
    malformed means the executor was never handed a step it could run.
    """
    for detail in error.errors():
        if tuple(detail.get("loc") or ()) == _ACTION_KIND_LOCATION:
            return REASON_UNKNOWN_ACTION
    return REASON_STEP_ERROR
