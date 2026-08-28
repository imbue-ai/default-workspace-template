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

from enum import auto

from pydantic import Field
from pydantic import ValidationError

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
# An action kind the script cannot perform. The driver decides the vocabulary, so this is the
# harness contradicting itself and nothing to do with the app.
REASON_UNKNOWN_ACTION = "unknown_action"
# Anything else that came out of the executor: a closed target, a dropped browser, a protocol
# error, a bug in the script. Unknown, but unknown on THIS side of the glass.
REASON_STEP_ERROR = "step_error"


class StepActionKind(LowerCaseStrEnum):
    """What one step does to the page.

    NOOP performs nothing and lets the capture report the page as it stands, which is how a caller
    takes a look without acting. RELOAD is its own kind rather than a re-OPEN: a reload keeps the
    URL and the session, which is exactly what a persistence flow is testing.
    """

    NOOP = auto()
    OPEN = auto()
    RELOAD = auto()
    CLICK = auto()
    INPUT = auto()
    KEYS = auto()
    SCROLL = auto()


class StepAction(FrozenModel):
    """One decided browser action.

    Elements are addressed by ARIA role and accessible name, which is what the page snapshot is
    expressed in and what survives the page changing underneath the agent.
    """

    kind: StepActionKind = Field(description="Which browser operation to perform")
    role: str = Field(default="", description="The target element's ARIA role, e.g. 'button'")
    target: str = Field(default="", description="The target element's accessible name")
    text: str = Field(default="", description="Text to type, keys to press, or the URL to open")
    amount: int = Field(default=0, description="Scroll distance in pixels; negative scrolls up")


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
