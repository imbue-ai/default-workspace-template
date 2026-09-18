"""One UI-flow step, executed in the box against the delivered app's forwarded origin.

Runs as a one-shot process per step: connects to the long-lived headless Chromium over CDP,
performs the requested action, captures the page, and prints a single JSON object on stdout. The
browser outlives it, so cookies, storage and the open page persist across steps without this script
holding any state or the driver holding a long-lived command protocol.

Uploaded into the box at trial time rather than baked into the image (the box_reverse_tunnel.py
pattern), so iterating on it never rebuilds the box image. flow_step_protocol.py, which
carries the request and result models both sides share, is uploaded beside it.

Invoked as: box_flow_step.py '<json StepRequest>'
Every outcome -- including failure -- is reported as a StepResult on stdout, because the caller
classifies by the reported reason and a traceback on stderr would read as a bridge failure instead.
"""

import os
import sys
import time
from typing import Any
from typing import assert_never

from flow_step_protocol import EXPECTED_REACTION_CAP_MS
from flow_step_protocol import POSSIBLE_REACTION_CAP_MS
from flow_step_protocol import QUIET_MS
from flow_step_protocol import REASON_ACTION_TIMED_OUT
from flow_step_protocol import REASON_CDP_CONNECT_FAILED
from flow_step_protocol import REASON_FORWARD_UNREACHABLE
from flow_step_protocol import REASON_STALE_REF
from flow_step_protocol import REASON_STEP_ERROR
from flow_step_protocol import REASON_TLS_REFUSED
from flow_step_protocol import REASON_TUNNEL_DOWN
from flow_step_protocol import REASON_UNKNOWN_ACTION
from flow_step_protocol import SETTLE_CAP_MS
from flow_step_protocol import StepAction
from flow_step_protocol import StepActionKind
from flow_step_protocol import StepCookie
from flow_step_protocol import StepReaction
from flow_step_protocol import StepRequest
from flow_step_protocol import StepResult
from flow_step_protocol import WAIT_ACTION_CAP_MS
from flow_step_protocol import request_error_reason
from flow_step_protocol import snapshot_line_for_ref
from flow_step_protocol import snapshot_line_role
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


class StaleRefError(Exception):
    """A ref the action addressed does not name what the agent read it off: the element is gone,
    or the page changed and the number now sits on something else. The action did not run; the
    page below is captured as it stands, and the flow carries on from what it now shows."""


class ReactionWatchError(Exception):
    """The script that watches the page for a reaction threw inside the browser.

    That script is this file's own JavaScript running in a world of its own, so an exception there
    is a bug in the instrument and is reported as one, never as the app failing to react.
    """


# How the page is rendered for the verification agent. "ai" is Playwright's denser LLM-oriented
# rendering of the same ARIA tree, which is what the host-side loop reads.
_SNAPSHOT_MODE = "ai"
_DEFAULT_TIMEOUT_MS = 15_000
# A navigation waits for the network to settle, since an app that renders from a fetch has nothing
# on the page at DOMContentLoaded. Capped well under the step's own budget.
_NAVIGATION_TIMEOUT_MS = 30_000
# How long a navigation an action started, but that has not replaced the document yet, is given to
# do so. It covers a server that takes its time to answer, since a document commits on the response
# and not on the request; when it expires, nothing is coming and the page as it stands is the one to
# read, which is the only case that pays this wait.
_NAVIGATION_COMMIT_TIMEOUT_MS = 10_000
_NAVIGATION_COMMIT_POLL_MS = 50
# How much of an error's text travels back: enough to diagnose, bounded so one exception cannot
# crowd the page state out of the flow log.
_MAX_DETAIL_CHARS = 2000
# The eight bytes every PNG file opens with, which is what the frame Playwright wrote should be.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# The reaction watch runs in an isolated world of its own on the page's main frame, the way
# Playwright's own instrumentation does: it shares the document, so it sees every mutation, but not
# the page's JavaScript globals, so nothing of it is visible to the app and the app cannot interfere
# with it. Two evaluations in that world bracket the action -- an observer installed before it, a
# waiter run after it -- so a reaction that lands synchronously inside the click handler is counted
# rather than missed.
_REACTION_WORLD_NAME = "minds-evals-reaction-watch"
_INSTALL_OBSERVER_JS = """
(() => {
  const state = { mutationCount: 0, firstMutationAt: 0, lastMutationAt: 0 };
  const observer = new MutationObserver((records) => {
    const now = performance.now();
    if (state.mutationCount === 0) state.firstMutationAt = now;
    state.mutationCount += records.length;
    state.lastMutationAt = now;
  });
  observer.observe(document.documentElement, {
    subtree: true, childList: true, attributes: true, characterData: true,
  });
  globalThis.__mindsEvalsReactionWatch = { state, observer };
})()
"""
# Resolves to a StepReaction value. Polled rather than driven by the observer itself, because the
# verdict depends on time passing with NO callbacks: the observer cannot announce its own silence.
#
# A verdict that rests on silence ("none", "settled") is only returned when the NEXT tick still sees
# no new mutation. Silence is read off the clock, and the clock keeps running while the renderer is
# not: a renderer the OS deschedules, or a main thread a long task holds, runs nothing for that
# long, so the page's own timers fall overdue beside the tick and the first tick to run afterwards
# sees a gap the page never had. Overdue timers were due before the tick this one schedules, so
# they run -- and their mutations reach the observer -- before that second look.
_WAIT_FOR_REACTION_JS = """
new Promise((resolve) => {
  const watch = globalThis.__mindsEvalsReactionWatch;
  const startedAt = performance.now();
  let silentAtCount = -1;
  const settleWith = (verdict) => { watch.observer.disconnect(); resolve(verdict); };
  const tick = () => {
    const now = performance.now();
    const state = watch.state;
    let silentVerdict = "";
    if (state.mutationCount === 0) {
      if (now - startedAt >= %(first_cap_ms)d) silentVerdict = "none";
    } else if (now - state.lastMutationAt >= %(quiet_ms)d) {
      silentVerdict = "settled";
    } else if (now - Math.max(state.firstMutationAt, startedAt) >= %(settle_cap_ms)d) {
      return settleWith("still_changing");
    }
    if (silentVerdict && silentAtCount === state.mutationCount) return settleWith(silentVerdict);
    silentAtCount = silentVerdict ? state.mutationCount : -1;
    setTimeout(tick, %(poll_ms)d);
  };
  tick();
})
"""
_REACTION_POLL_MS = 25
# What Chromium answers when the document the watch's world belongs to is gone. All three spellings
# are the same event and all three happen: the first when the world was already gone as the
# evaluation was dispatched, the other two when it died with an evaluation waiting on it -- an app
# that reacts and only then redirects. A world dies with its document, so after an action this means
# one thing: the action navigated. A target that was genuinely closed rather than navigated fails
# instead on the load-state wait that follows, and is reported from there.
_CONTEXT_GONE_MARKERS = (
    "cannot find context",
    "execution context was destroyed",
    "inspected target navigated or closed",
)


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

    Type is what separates the things a step can mean, and prose cannot: a timeout is the page
    failing to offer what was asked for (the app's shortfall), a stale ref is the agent addressing
    a page that has moved on, an unknown action kind is the harness contradicting itself, and
    everything else out of the executor -- the reaction watch included -- is the executor. Only
    within a Playwright error does the message get a say, and only to name which transport hop
    broke -- an unrecognised one stays an executor failure rather than being charged to the app.
    """
    if isinstance(exc, UnknownActionError):
        return REASON_UNKNOWN_ACTION
    if isinstance(exc, StaleRefError):
        return REASON_STALE_REF
    if isinstance(exc, PlaywrightTimeoutError):
        return REASON_ACTION_TIMED_OUT
    if isinstance(exc, PlaywrightError):
        return _transport_reason(str(exc)) or REASON_STEP_ERROR
    return REASON_STEP_ERROR


def reaction_cap_ms(kind: StepActionKind) -> int | None:
    """How long a step of this kind waits for the page to start reacting, or None when it does not
    watch: a navigation waits for the network instead, and a bare read has nothing to react to."""
    match kind:
        case StepActionKind.NOOP | StepActionKind.OPEN | StepActionKind.RELOAD:
            return None
        case StepActionKind.CLICK | StepActionKind.KEYS:
            return EXPECTED_REACTION_CAP_MS
        case StepActionKind.INPUT | StepActionKind.SCROLL:
            return POSSIBLE_REACTION_CAP_MS
        case StepActionKind.WAIT:
            return WAIT_ACTION_CAP_MS
        case _ as unreachable:
            assert_never(unreachable)


def _bounded(exc: BaseException) -> str:
    return str(exc)[:_MAX_DETAIL_CHARS]


def _playwright_cookie(cookie: StepCookie) -> dict[str, Any]:
    """The cookie in the shape `add_cookies` wants. The third-party spelling lives here only."""
    return {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path,
        "httpOnly": cookie.is_http_only,
        "secure": cookie.is_secure,
        "sameSite": cookie.same_site,
    }


def _locate(page: Any, action: StepAction, default_role: str) -> Any:
    """The element an action addresses: by role and name, or -- for an element the snapshot listed
    with no name -- by the ref that snapshot printed for it.

    A ref is only meaningful against the snapshot that assigned it, and refs live in the connection
    that took that snapshot, so this connection takes a snapshot of its own first: numbering runs in
    document order, so an unchanged page gets the same numbers the agent read. The role the ref was
    read on -- which every ref carries, since a ref without one is refused at the boundary -- is
    checked against that fresh snapshot, because a page that changed in between can leave the
    number on a different element, and acting on it would put a click the agent never asked for
    into the record.
    """
    if not action.ref:
        return page.get_by_role(action.role or default_role, name=action.target).first
    line = snapshot_line_for_ref(page.aria_snapshot(mode=_SNAPSHOT_MODE), action.ref)
    if not line:
        raise StaleRefError(
            "ref {} is not on the page any more; address the element from the current page state".format(action.ref)
        )
    role = snapshot_line_role(line)
    if role != action.role:
        raise StaleRefError(
            "ref {} names a {} now, not a {}: the page changed; address the element from the current page state".format(
                action.ref, role or "non-element", action.role
            )
        )
    return page.locator("aria-ref={}".format(action.ref))


def _perform(page: Any, action: StepAction) -> None:
    """Carry out one decided action. A kind with no branch here raises rather than silently doing
    nothing: the caller reports that as the harness contradicting itself, not as the app failing."""
    if action.kind is StepActionKind.NOOP or action.kind is StepActionKind.WAIT:
        # Perform nothing and let the capture report the page as it stands. NOOP is how a caller
        # takes a look without acting; WAIT is the same look, after the reaction watch has given
        # the page its time.
        return
    if action.kind is StepActionKind.OPEN:
        page.goto(action.text, wait_until="networkidle", timeout=_NAVIGATION_TIMEOUT_MS)
        return
    if action.kind is StepActionKind.RELOAD:
        page.reload(wait_until="networkidle", timeout=_NAVIGATION_TIMEOUT_MS)
        return
    if action.kind is StepActionKind.CLICK:
        _locate(page, action, "button").click(timeout=_DEFAULT_TIMEOUT_MS)
        return
    if action.kind is StepActionKind.INPUT:
        _locate(page, action, "textbox").fill(action.text, timeout=_DEFAULT_TIMEOUT_MS)
        return
    if action.kind is StepActionKind.KEYS:
        page.keyboard.press(action.text or "Enter")
        return
    if action.kind is StepActionKind.SCROLL:
        page.mouse.wheel(0, action.amount or 500)
        return
    raise UnknownActionError("no branch performs action kind {!r}".format(action.kind.value))


def _evaluate_in_world(cdp_session: Any, context_id: int, expression: str, is_promise: bool) -> Any:
    """Run one expression in the reaction watch's world and return its value."""
    reply = cdp_session.send(
        "Runtime.evaluate",
        {
            "expression": expression,
            "contextId": context_id,
            "awaitPromise": is_promise,
            "returnByValue": True,
        },
    )
    if "exceptionDetails" in reply:
        raise ReactionWatchError(str(reply["exceptionDetails"].get("text") or reply["exceptionDetails"]))
    return reply["result"].get("value")


def _install_reaction_watch(cdp_session: Any, frame_id: str) -> int:
    """Create the watch's world on the main frame, install the observer there, and return the
    world's context id for the waiter to address."""
    context_id = cdp_session.send(
        "Page.createIsolatedWorld", {"frameId": frame_id, "worldName": _REACTION_WORLD_NAME}
    )["executionContextId"]
    _evaluate_in_world(cdp_session, context_id, _INSTALL_OBSERVER_JS, is_promise=False)
    return context_id


def _is_context_gone(exc: PlaywrightError) -> bool:
    """Whether a protocol error says the world the evaluation named no longer exists."""
    lowered = str(exc).lower()
    return any(marker in lowered for marker in _CONTEXT_GONE_MARKERS)


def _main_frame(cdp_session: Any) -> dict[str, Any]:
    """The page's main frame, carrying its own id and the id of the document it is showing."""
    return cdp_session.send("Page.getFrameTree")["frameTree"]["frame"]


def _main_frame_loader_id(cdp_session: Any) -> str:
    """The id of the document the main frame is showing, which changes with every document it loads.

    What a document is identified by here, rather than its url: a navigation to the SAME url is a
    new document too, and an app that saves and reloads itself makes exactly that one.
    """
    return str(_main_frame(cdp_session)["loaderId"])


def _settle_after_navigation(page: Any, cdp_session: Any, loader_id_before: str) -> None:
    """Wait for the document the action navigated to, and for its network to go quiet.

    A world that dies while the waiter is evaluating in it is Chromium failing that evaluation as the
    navigation STARTS: the new document has not committed yet, so the page here is still the one the
    action is about to replace, and waiting on its load state alone would return at once and have the
    capture record the page the flow just left as if it were the one it reached. So the commit is
    waited for first. A navigation that never commits -- one the page abandoned -- leaves the page as
    it stands, which is then the page to read.
    """
    # Polled rather than awaited as a `framenavigated` event: that event also fires for a
    # same-document navigation (a pushState on the way out), which would end the wait on the old
    # document, and an event that lands before its listener is registered is never seen at all.
    deadline = time.monotonic() + _NAVIGATION_COMMIT_TIMEOUT_MS / 1000
    while _main_frame_loader_id(cdp_session) == loader_id_before and time.monotonic() < deadline:
        page.wait_for_timeout(_NAVIGATION_COMMIT_POLL_MS)
    page.wait_for_load_state("networkidle", timeout=_NAVIGATION_TIMEOUT_MS)


def _wait_for_reaction(
    page: Any, cdp_session: Any, context_id: int, first_cap_ms: int, loader_id_before: str
) -> StepReaction:
    """What the DOM did after the action, read once it has settled or the caps have run out."""
    waiter = _WAIT_FOR_REACTION_JS % {
        "first_cap_ms": first_cap_ms,
        "quiet_ms": QUIET_MS,
        "settle_cap_ms": SETTLE_CAP_MS,
        "poll_ms": _REACTION_POLL_MS,
    }
    try:
        verdict = _evaluate_in_world(cdp_session, context_id, waiter, is_promise=True)
    except PlaywrightError as exc:
        if not _is_context_gone(exc):
            raise
        # The world died with its document, so the action navigated. The new document is the
        # reaction, read the way an explicit navigation is read: once its network has settled.
        _settle_after_navigation(page, cdp_session, loader_id_before)
        return StepReaction.SETTLED
    return StepReaction(verdict)


def _perform_and_watch(page: Any, cdp_session: Any, action: StepAction) -> StepReaction:
    """Carry out the action and, for the kinds that watch, wait for the page's reaction to it.

    The observer goes in BEFORE the action so a reaction that lands inside the event handler is
    counted; a watch installed afterwards would report the commonest case -- a synchronous render
    -- as no reaction at all.
    """
    first_cap_ms = reaction_cap_ms(action.kind)
    if first_cap_ms is None:
        _perform(page, action)
        return StepReaction.UNOBSERVED
    # One read of the frame tree serves both: creating an isolated world loads no document, so the
    # loader id on this reply is still the one standing when the action runs.
    frame = _main_frame(cdp_session)
    context_id = _install_reaction_watch(cdp_session, str(frame["id"]))
    loader_id_before = str(frame["loaderId"])
    _perform(page, action)
    return _wait_for_reaction(page, cdp_session, context_id, first_cap_ms, loader_id_before)


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
                # authenticated.
                context.add_cookies([_playwright_cookie(request.cookie)])
            page = _resolve_page(context)
            reason = ""
            detail = ""
            reaction = StepReaction.UNOBSERVED
            try:
                reaction = _perform_and_watch(page, context.new_cdp_session(page), request.action)
            except (PlaywrightError, UnknownActionError, ReactionWatchError, StaleRefError) as exc:
                # The action failed, but the PAGE is still readable -- and what it shows is the
                # most useful thing the flow can record, so capture it before returning.
                reason = classify_exception(exc)
                detail = _bounded(exc)
            if request.action.ref:
                try:
                    browser, page = _reconnected_page(playwright, browser, request.cdp_endpoint)
                except (PlaywrightError, MissingContextError) as exc:
                    # Reconnecting is this script talking to its own browser, so a failure here is
                    # the CDP endpoint and nothing the forwarded origin did -- which is what
                    # `classify_exception` would read out of the message text instead. There is no
                    # connection left to capture the page from, and the action's own verdict, where
                    # it has one, stays the informative one.
                    return StepResult(
                        is_ok=False,
                        reason=reason or REASON_CDP_CONNECT_FAILED,
                        detail=detail or _bounded(exc),
                        reaction=reaction,
                    )
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
                screenshot_byte_count=capture.screenshot_byte_count,
                is_screenshot_png=capture.is_screenshot_png,
                reaction=reaction,
            )
        finally:
            # Only the CDP connection is closed. Closing the browser would end the session the next
            # step depends on.
            browser.close()


def _reconnected_page(playwright: Any, browser: Any, cdp_endpoint: str) -> tuple[Any, Any]:
    """A connection of this script's own in place of `browser`, and the page it drives.

    A ref lookup numbers the connection that took the snapshot, and a later snapshot on that same
    connection keeps those numbers and continues them for whatever is new. The next step numbers
    the page afresh, so the capture the agent reads its next ref off must too.
    """
    browser.close()
    reconnected = playwright.chromium.connect_over_cdp(cdp_endpoint)
    try:
        return reconnected, _resolve_page(_resolve_context(reconnected))
    except (PlaywrightError, MissingContextError):
        # The caller only ever holds the connection this returns, so one that fails on the way out
        # has to close itself or nothing will.
        reconnected.close()
        raise


def _resolve_page(context: Any) -> Any:
    """The page every step drives: the context's first, opened by the flow's first step."""
    return context.pages[0] if context.pages else context.new_page()


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
    byte_count, is_png = _read_frame_facts(written_path) if written_path else (0, False)
    return StepResult(
        is_ok=not reason,
        reason=reason,
        detail=detail,
        url=url,
        title=title,
        snapshot=snapshot,
        screenshot_path=written_path,
        screenshot_byte_count=byte_count,
        is_screenshot_png=is_png,
    )


def _read_frame_facts(frame_path: str) -> tuple[int, bool]:
    """The written frame's size and whether it starts with the PNG signature, read back from the file
    itself; (0, False) when it cannot be read, which is a frame nobody can look at either."""
    try:
        with open(frame_path, "rb") as frame:
            head = frame.read(len(_PNG_SIGNATURE))
            byte_count = frame.seek(0, os.SEEK_END)
    except OSError:
        return 0, False
    return byte_count, head == _PNG_SIGNATURE


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
