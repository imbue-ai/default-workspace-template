"""One UI-flow step, executed in the box against the delivered app's forwarded origin.

Runs as a one-shot process per step: connects to the long-lived headless Chromium over CDP,
performs the requested action, captures the page, and prints a single JSON object on stdout. The
browser outlives it, so cookies, storage and the open page persist across steps without this script
holding any state or the driver holding a long-lived command protocol.

Uploaded into the box at trial time rather than baked into the image (the box_reverse_tunnel.py
pattern), so iterating on it never invalidates the image layer cache. flow_step_protocol.py, which
carries the request and result models both sides share, is uploaded beside it.

Invoked as: box_flow_step.py '<json StepRequest>'
Every outcome -- including failure -- is reported as a StepResult on stdout, because the caller
classifies by the reported reason and a traceback on stderr would read as a bridge failure instead.
"""

import sys
from typing import Any

from flow_step_protocol import REASON_ACTION_TIMED_OUT
from flow_step_protocol import REASON_CDP_CONNECT_FAILED
from flow_step_protocol import REASON_FORWARD_UNREACHABLE
from flow_step_protocol import REASON_STEP_ERROR
from flow_step_protocol import REASON_TLS_REFUSED
from flow_step_protocol import REASON_TUNNEL_DOWN
from flow_step_protocol import REASON_UNKNOWN_ACTION
from flow_step_protocol import StepAction
from flow_step_protocol import StepActionKind
from flow_step_protocol import StepCookie
from flow_step_protocol import StepRequest
from flow_step_protocol import StepResult
from flow_step_protocol import request_error_reason
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from pydantic import ValidationError


class UnknownActionError(Exception):
    """An action kind this script has no way to perform.

    The driver and this script share one action vocabulary; a kind that arrives here without a
    branch below means the two have drifted apart, which is a harness fault and is reported as one.
    """


class MissingContextError(Exception):
    """The connected browser exposes no context at all -- an instrument failure, not an app one."""


# How the page is rendered for the verification agent. "ai" is Playwright's denser LLM-oriented
# rendering of the same ARIA tree, which is what the host-side loop reads.
_SNAPSHOT_MODE = "ai"
_DEFAULT_TIMEOUT_MS = 15_000
# A navigation waits for the network to settle, since an app that renders from a fetch has nothing
# on the page at DOMContentLoaded. Capped well under the step's own budget.
_NAVIGATION_TIMEOUT_MS = 30_000
# How much of an error's text travels back: enough to diagnose, bounded so one exception cannot
# crowd the page state out of the flow log.
_MAX_DETAIL_CHARS = 2000


def _transport_reason(message: str) -> str:
    """Which transport layer an error's text implicates, or empty when it names none.

    Playwright reports transport problems as prose, so this is substring work, but the distinctions
    it draws are the ones the manifest needs: the proxy not being there at all, its TLS refusing,
    and the proxy answering while the workspace leg behind it is dead.
    """
    lowered = message.lower()
    if "err_connection_refused" in lowered or "econnrefused" in lowered:
        return REASON_FORWARD_UNREACHABLE
    if "err_ssl" in lowered or "err_cert" in lowered or "ssl" in lowered:
        return REASON_TLS_REFUSED
    # The proxy answers 503 with its own loading page when it cannot reach the workspace, so a
    # navigation "succeeds" and the failure is only visible in the status.
    if "err_empty_response" in lowered or "err_connection_reset" in lowered:
        return REASON_TUNNEL_DOWN
    return ""


def classify_exception(exc: BaseException) -> str:
    """Which layer a failure implicates, decided by TYPE and only then by text.

    Type is what separates the three things a step can mean, and prose cannot: a timeout is the
    page failing to offer what was asked for (the app's shortfall), an unknown action kind is the
    harness contradicting itself, and everything else out of the executor is the executor. Only
    within a Playwright error does the message get a say, and only to name which transport hop
    broke -- an unrecognised one stays an executor failure rather than being charged to the app.
    """
    if isinstance(exc, UnknownActionError):
        return REASON_UNKNOWN_ACTION
    if isinstance(exc, PlaywrightTimeoutError):
        return REASON_ACTION_TIMED_OUT
    if isinstance(exc, PlaywrightError):
        return _transport_reason(str(exc)) or REASON_STEP_ERROR
    return REASON_STEP_ERROR


def _bounded(exc: BaseException) -> str:
    return str(exc)[:_MAX_DETAIL_CHARS]


def _playwright_cookie(cookie: StepCookie) -> dict[str, Any]:
    """The cookie in the shape `add_cookies` wants. The third-party spelling lives here only."""
    return {
        "name": cookie.name,
        "value": cookie.value,
        "url": cookie.url,
        "httpOnly": cookie.is_http_only,
        "secure": cookie.is_secure,
        "sameSite": cookie.same_site,
    }


def _perform(page: Any, action: StepAction) -> None:
    """Carry out one decided action. A kind with no branch here raises rather than silently doing
    nothing: the caller reports that as the harness contradicting itself, not as the app failing."""
    if action.kind is StepActionKind.NOOP:
        # Perform nothing and let the capture report the page as it stands. That is how a caller
        # takes a look without acting.
        return
    if action.kind is StepActionKind.OPEN:
        page.goto(action.text, wait_until="networkidle", timeout=_NAVIGATION_TIMEOUT_MS)
        return
    if action.kind is StepActionKind.RELOAD:
        page.reload(wait_until="networkidle", timeout=_NAVIGATION_TIMEOUT_MS)
        return
    if action.kind is StepActionKind.CLICK:
        page.get_by_role(action.role or "button", name=action.target).first.click(timeout=_DEFAULT_TIMEOUT_MS)
        return
    if action.kind is StepActionKind.INPUT:
        page.get_by_role(action.role or "textbox", name=action.target).first.fill(
            action.text, timeout=_DEFAULT_TIMEOUT_MS
        )
        return
    if action.kind is StepActionKind.KEYS:
        page.keyboard.press(action.text or "Enter")
        return
    if action.kind is StepActionKind.SCROLL:
        page.mouse.wheel(0, action.amount or 500)
        return
    raise UnknownActionError("no branch performs action kind {!r}".format(action.kind.value))


def run_step(request: StepRequest) -> StepResult:
    """Connect, act, capture. Returns the result printed on stdout."""
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(request.cdp_endpoint)
        except PlaywrightError as exc:
            return StepResult(is_ok=False, reason=REASON_CDP_CONNECT_FAILED, detail=_bounded(exc))
        try:
            try:
                context = _resolve_context(browser)
            except MissingContextError as exc:
                return StepResult(is_ok=False, reason=REASON_CDP_CONNECT_FAILED, detail=_bounded(exc))
            if request.cookie is not None:
                # Installed before the flow's first navigation, so the opening request is already
                # authenticated and never takes the proxy's login redirect.
                context.add_cookies([_playwright_cookie(request.cookie)])
            page = context.pages[0] if context.pages else context.new_page()
            reason = ""
            detail = ""
            try:
                _perform(page, request.action)
            except (PlaywrightError, UnknownActionError) as exc:
                # The action failed, but the PAGE is still readable -- and what it shows is the
                # most useful thing the flow can record, so capture it before returning.
                reason = classify_exception(exc)
                detail = _bounded(exc)
            capture = _capture(page, request.screenshot_path)
            # A page that could not be read only gets to speak when the action itself said nothing:
            # the first failure is the informative one.
            reason = reason or capture.reason
            detail = detail or capture.detail
            return StepResult(
                is_ok=not reason,
                reason=reason,
                detail=detail,
                url=capture.url,
                title=capture.title,
                snapshot=capture.snapshot,
                screenshot_path=capture.screenshot_path,
            )
        finally:
            # Only the CDP connection is closed. Closing the browser would end the session the next
            # step depends on.
            browser.close()


def _resolve_context(browser: Any) -> Any:
    """The browser's OWN default context -- never one this script creates.

    Playwright creates a CDP browser context with `disposeOnDetach`, so a context made here would
    die with this one-shot process and the next step would find it gone. Everything a flow needs to
    persist -- cookies, storage, the open page -- therefore lives in the default context, which the
    browser process owns; isolation between flows comes from a separate browser per flow.
    """
    contexts = browser.contexts
    if not contexts:
        raise MissingContextError("the browser reports no context to drive")
    return contexts[0]


def _capture(page: Any, screenshot_path: str) -> StepResult:
    """What the page is now: its URL, title, ARIA tree, and a screenshot.

    Carried in a StepResult because those are exactly its fields; the caller merges it with
    whatever the action itself had to say.
    """
    reason = ""
    detail = ""
    url = ""
    title = ""
    snapshot = ""
    try:
        url = page.url
        title = page.title()
        snapshot = page.aria_snapshot(mode=_SNAPSHOT_MODE)
    except PlaywrightError as exc:
        reason = classify_exception(exc)
        detail = _bounded(exc)
    written_path = ""
    if screenshot_path:
        try:
            page.screenshot(path=screenshot_path, timeout=_DEFAULT_TIMEOUT_MS)
            written_path = screenshot_path
        except PlaywrightError:
            # A missing frame costs the judge one image; it must never cost the step its verdict.
            written_path = ""
    return StepResult(
        is_ok=not reason,
        reason=reason,
        detail=detail,
        url=url,
        title=title,
        snapshot=snapshot,
        screenshot_path=written_path,
    )


def _result_for(argv: list[str]) -> StepResult:
    """The reply to whatever this process was handed, including being handed nothing usable."""
    try:
        request = StepRequest.model_validate_json(argv[1])
    except IndexError:
        return StepResult(is_ok=False, reason=REASON_STEP_ERROR, detail="no request argument")
    except ValidationError as exc:
        return StepResult(is_ok=False, reason=request_error_reason(exc), detail=_bounded(exc))
    try:
        return run_step(request)
    except Exception as exc:
        # Never let a traceback reach stderr instead of a verdict: the caller reads stdout JSON and
        # would otherwise classify a bug here as the bridge failing.
        return StepResult(is_ok=False, reason=classify_exception(exc), detail=_bounded(exc))


def main() -> int:
    # The one thing this process says on stdout, success or failure.
    print(_result_for(sys.argv).model_dump_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
