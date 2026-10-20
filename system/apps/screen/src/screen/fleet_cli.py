"""``agentic-browser-fleet``: own the browser on the Screen from an agent's shell (spec section 16.3).

This is the CLI the agent calls to own a browser, not to drive one: a thin, stateless HTTP client to the Screen
service's fleet routes. Driving happens over ``playwright-cli``, pointed at the gated CDP URL that ``new`` and
``acquire`` print. Taking control is a human action in the Screen's viewer, not something this CLI does.

Exit codes, which an agent branches on: 0 ok, 1 error, 2 preempted (a human has or took control; end the turn),
3 busy (another agent holds it, or the browser is still starting), 4 timed out, 64 usage, 69 no service.
"""

import json
import os
import threading
import urllib.error
import urllib.request
from typing import Any
from typing import Final

import click
from loguru import logger

from imbue.imbue_common.pure import pure
from screen.cli import EXIT_BUSY
from screen.cli import EXIT_ERROR
from screen.cli import EXIT_NO_SERVICE
from screen.cli import EXIT_OK
from screen.cli import EXIT_PREEMPTED
from screen.cli import EXIT_TIMEOUT
from screen.cli import EXIT_USAGE
from screen.cli import open_screen_window
from screen.cli import own_chat_id
from screen.cli import service_url
from screen.identity import AGENT_ID_HEADER
from screen.identity import AGENT_NAME_HEADER

_REQUEST_TIMEOUT_SECONDS: Final[float] = 90.0
# ``new`` answers as soon as the browser is registered; the stack and Chromium come up behind it, so the attach
# URL is polled for a while before the agent is told to come back.
_ATTACH_ATTEMPTS: Final[int] = 120
_ATTACH_POLL_SECONDS: Final[float] = 0.5
HTTP_BAD_REQUEST: Final[int] = 400
HTTP_NOT_FOUND: Final[int] = 404
HTTP_CONFLICT: Final[int] = 409
HTTP_SERVICE_UNAVAILABLE: Final[int] = 503


def _out(message: str) -> None:
    click.echo(message)


def _err(message: str) -> None:
    click.echo(message, err=True)


def agent_headers() -> dict[str, str]:
    """The identity headers; a hard failure with no chat id to send (no null owner)."""
    chat_id = own_chat_id()
    if not chat_id:
        _err("MNGR_AGENT_ID is not set -- run agentic-browser-fleet from inside an agent.")
        raise SystemExit(EXIT_USAGE)
    headers = {AGENT_ID_HEADER: chat_id, "Content-Type": "application/json"}
    name = os.environ.get("MNGR_AGENT_NAME")
    if name:
        headers[AGENT_NAME_HEADER] = name
    return headers


def request(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """One JSON request to the Screen service; answers (status, body)."""
    url = service_url() + path
    data = json.dumps(body).encode() if body is not None else None
    http_request = urllib.request.Request(url, data=data, method=method, headers=agent_headers())
    try:
        with urllib.request.urlopen(http_request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, _parse(response.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read()) or {"detail": e.reason}
    except urllib.error.URLError as e:
        _err(f"cannot reach the Screen service at {service_url()} ({e.reason}). Is it running?")
        raise SystemExit(EXIT_NO_SERVICE) from e


# CLEANUP: drop the ``error`` fallback once no Screen service that answers a refusal as ``{"error": ...}`` can
# still be running under an updated workspace tree, that is once every workspace has restarted the service on the
# typed routes (the ones that answer every refusal as ``{"detail": ...}``).
@pure
def refusal_reason(payload: dict[str, Any], fallback: str) -> str:
    """Why the service refused, in its own words: ``detail``, the shape of every refusal; ``error`` from a service
    not yet restarted on the typed routes; else ``fallback``."""
    return str(payload.get("detail") or payload.get("error") or fallback)


@pure
def _parse(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@pure
def owner_label(browser: dict[str, Any], me: str | None) -> str:
    """Who holds the browser, in ``ls``'s words."""
    if browser.get("crashed") or browser.get("lifecycle") == "crashed":
        return "crashed (gone -- start it again with `new`)"
    if browser.get("lifecycle") == "init":
        return "starting (Chromium launching -- ready shortly)"
    if browser.get("lifecycle") == "stopped":
        return "stopped (start it again with `new`)"
    if browser.get("controller") == "agent":
        name = browser.get("owner_name") or browser.get("owner_agent_id") or "?"
        return "you" if browser.get("owner_agent_id") == me else f"agent {name}"
    if browser.get("controller") == "human":
        return "human (took control)" if browser.get("human_pinned") else "human (driving the Screen)"
    return "free"


@pure
def render_action(payload: dict[str, Any], browser_name: str, kind: str) -> tuple[str, int, bool]:
    """The line to print, the exit code, and whether the line is for stdout, for an ownership verb's answer. The
    exit code is the whole point: ``playwright-cli`` exits 1 for any error, so ``acquire`` is what an agent branches
    on before it attaches."""
    if payload.get("ok"):
        return f"ok: {kind}", EXIT_OK, True
    status = payload.get("status")
    is_enqueued = bool(payload.get("enqueued"))
    if status == "busy_human":
        if is_enqueued:
            return (
                f"browser {browser_name}: the human has control of the Screen -- you're queued to resume and will be "
                "messaged when they hand it back (if you don't hear back in a while, re-run `ls` to check). Tell the "
                "user, then end your turn.",
                EXIT_PREEMPTED,
                True,
            )
        return (
            f"the human is controlling the Screen right now, so browser {browser_name} is theirs. Re-run `ls` later to "
            "check, or ask the user.",
            EXIT_BUSY,
            True,
        )
    if status == "busy_agent":
        if is_enqueued:
            return (
                f"browser {browser_name} is held by another agent -- you're queued for it and will be messaged when it "
                "frees (if you don't hear back in a while, re-run `ls` to check).",
                EXIT_BUSY,
                True,
            )
        return f"another agent is using browser {browser_name} right now. Re-run `ls` later to check.", EXIT_BUSY, True
    if status == "starting":
        return (
            f"browser {browser_name} is still starting up (the Screen or Chromium is launching) -- try again in a few "
            "seconds.",
            EXIT_BUSY,
            False,
        )
    if status == "crashed":
        return (
            f"browser {browser_name} crashed (Chromium was killed -- e.g. out of memory). Start it again with `new`.",
            EXIT_ERROR,
            False,
        )
    if status == "stopped":
        return (
            str(payload.get("hint") or f"browser {browser_name} is stopped; start it again with `new`."),
            EXIT_ERROR,
            False,
        )
    if status == "timed_out":
        return f"browser {browser_name} stayed busy; gave up.", EXIT_TIMEOUT, False
    return refusal_reason(payload, f"command failed ({status})"), EXIT_ERROR, False


def _print_action(payload: dict[str, Any], browser_name: str, kind: str) -> int:
    line, code, is_stdout = render_action(payload, browser_name, kind)
    (_out if is_stdout else _err)(line)
    return code


def attach_url(
    browser_name: str, attempts: int = _ATTACH_ATTEMPTS, delay_seconds: float = _ATTACH_POLL_SECONDS
) -> str:
    """Poll for the browser's attach URL while it is starting; "" when it will not come (anything but ``starting``
    is not worth waiting on)."""
    pause = threading.Event()
    for attempt in range(attempts):
        status, payload = request("GET", f"/browsers/{browser_name}/attach")
        if status == 200 and payload.get("ok"):
            return str(payload.get("attach_url", ""))
        if status != 200 or payload.get("status") != "starting":
            return ""
        if attempt + 1 < attempts:
            pause.wait(delay_seconds)
    return ""


def _print_attach(browser_name: str) -> None:
    url = attach_url(browser_name)
    if url:
        _out(f"   drive it:  playwright-cli -s={browser_name} attach --cdp={url}")
        _out(
            f"   then:      playwright-cli -s={browser_name} <command>   (run `playwright-cli --help` for the commands)"
        )
    else:
        _out(f"   (still starting up -- run `ls` in a few seconds, then attach to browser {browser_name})")


@click.group()
def main() -> None:
    """Own the browser on the Screen: start it, list it, reserve it, hand it to the human, give it back."""
    # The command reports everything it has to say itself; the layout client's own logs would add lines no agent
    # reading this output expects.
    logger.remove()


@main.command("ls")
@click.option(
    "--include-tabs", "is_including_tabs", is_flag=True, default=False, help="List every tab, not just the active one."
)
def cmd_ls(is_including_tabs: bool) -> None:
    """List the browser, who holds it, and its tabs."""
    status, payload = request("GET", "/browsers")
    if status != 200:
        _err(refusal_reason(payload, f"ls failed ({status})"))
        raise SystemExit(EXIT_ERROR)
    browsers = payload.get("browsers", [])
    if not browsers:
        _out("no browser yet -- use `new` to start one (it prints a name to drive by)")
        return
    me = own_chat_id()
    for browser in browsers:
        tabs = browser.get("tabs", [])
        active = next((tab for tab in tabs if tab.get("active")), None)
        where = (active.get("url") or active.get("title") or "") if active else "(no tab)"
        waiting = browser.get("waiting") or []
        queued = f"  [queued: {', '.join(waiting)}]" if waiting else ""
        _out(f"browser {browser['id']}: {owner_label(browser, me)} -- {len(tabs)} tab(s), active: {where}{queued}")
        if is_including_tabs:
            for tab in tabs:
                mark = "*" if tab.get("active") else " "
                _out(f"    [{tab.get('index')}]{mark} {tab.get('title') or ''}  {tab.get('url', '')}")


@main.command("new")
@click.argument("name", required=False, default=None)
@click.option("--url", default=None, help="The page the browser opens on (an absolute http(s) URL).")
def cmd_new(name: str | None, url: str | None) -> None:
    """Start the browser (or answer the one that exists) and print its name and attach line."""
    body: dict[str, Any] = {"name": name} if name else {}
    if url:
        body["url"] = url
    status, payload = request("POST", "/browsers", body)
    if status == 200:
        # The browser shares the Screen: its window is the Screen's, opened minimized so it waits in the taskbar.
        open_screen_window(is_minimized=True)
        _out(f"started browser {payload['name']}")
        _print_attach(str(payload["name"]))
        return
    _err(refusal_reason(payload, f"new failed ({status})"))
    if status == HTTP_BAD_REQUEST:
        raise SystemExit(EXIT_USAGE)
    raise SystemExit(EXIT_BUSY if status in (HTTP_CONFLICT, HTTP_SERVICE_UNAVAILABLE) else EXIT_ERROR)


@main.command("close")
@click.argument("name")
def cmd_close(name: str) -> None:
    """End the browser and DELETE its profile (every login); for one tab use `playwright-cli tab-close`."""
    status, payload = request("DELETE", f"/browsers/{name}")
    if status != 200:
        _err(refusal_reason(payload, f"close failed ({status})"))
        raise SystemExit(EXIT_BUSY if status == HTTP_SERVICE_UNAVAILABLE else EXIT_ERROR)
    _out(f"closed browser {name}")


@main.command("acquire")
@click.argument("name")
@click.option(
    "--reclaim",
    is_flag=True,
    default=False,
    help="Take the Screen back from the human -- ONLY when they told you to continue.",
)
def cmd_acquire(name: str, reclaim: bool) -> None:
    """Reserve the browser (the Screen's lease) and reprint the attach line."""
    status, payload = request("POST", f"/browsers/{name}/acquire", {"reclaim": reclaim})
    if status == HTTP_NOT_FOUND:
        _err(refusal_reason(payload, f"no browser {name}"))
        raise SystemExit(EXIT_ERROR)
    if payload.get("ok"):
        open_screen_window(is_minimized=True)
        _out(f"acquired browser {name}")
        _print_attach(name)
        return
    raise SystemExit(_print_action(payload, name, "acquire"))


def _release(name: str) -> None:
    status, payload = request("POST", f"/browsers/{name}/release")
    if status != 200:
        _err(refusal_reason(payload, f"release failed ({status})"))
        raise SystemExit(EXIT_ERROR)
    _out(f"released browser {name}" if payload.get("released") else f"browser {name} was not yours to release")


@main.command("release")
@click.argument("name")
def cmd_release(name: str) -> None:
    """Hand the browser back (the Screen's lease is released)."""
    _release(name)


@main.command("unlock", hidden=True)
@click.argument("name")
def cmd_unlock(name: str) -> None:
    """An alias of `release`."""
    _release(name)


def _handoff(name: str, reason: str) -> None:
    status, payload = request("POST", f"/browsers/{name}/handoff", {"reason": reason})
    if status == HTTP_NOT_FOUND:
        _err(refusal_reason(payload, f"no browser {name}"))
        raise SystemExit(EXIT_ERROR)
    if payload.get("ok"):
        # The human has to act in it, so the Screen's window is shown rather than parked.
        is_shown = open_screen_window(is_minimized=False)
        _out(
            f"handed browser {name} to the human: {reason}. You're first in line to resume. Tell the user what to do"
            + ("" if is_shown else " (and to open the Screen themselves, since its window could not be shown)")
            + ", end your turn, and re-run `acquire` when they hand control back."
        )
        raise SystemExit(EXIT_PREEMPTED)
    if payload.get("status") == "not_owner":
        _out(
            f"browser {name} isn't yours to hand off -- a human may already control the Screen. Run `ls` to see who "
            "has it; if the human took over, you're queued to resume."
        )
        raise SystemExit(EXIT_PREEMPTED)
    raise SystemExit(_print_action(payload, name, "handoff"))


@main.command("handoff")
@click.argument("name")
@click.argument("reason", required=False, default="human verification needed")
def cmd_handoff(name: str, reason: str) -> None:
    """Hand the browser to the human for a CAPTCHA, a robot check or a login you cannot do, then stop."""
    _handoff(name, reason)


@main.command("request-human", hidden=True)
@click.argument("name")
@click.argument("reason", required=False, default="human verification needed")
def cmd_request_human(name: str, reason: str) -> None:
    """An alias of `handoff`."""
    _handoff(name, reason)


if __name__ == "__main__":
    main()
