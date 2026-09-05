"""``agentic-browser-fleet``: drive the shared browser fleet from an agent's shell.

This is the CLI the agent calls to OWN a browser -- not to drive one. It is a thin,
stateless HTTP client to the per-workspace browser daemon (runner.py). Driving happens
over ``playwright-cli``, pointed at the gated CDP URL that ``new`` and ``acquire``
print. ``take control`` is a human action in the UI, not something this CLI does.

Ownership rules (enforced by the daemon, surfaced here):

* Each browser is controlled by exactly one party. The lease is sticky: it is acquired
  on first use and auto-released after an idle timeout.
* Agents never preempt each other: ``acquire`` on a browser another agent holds
  waits in a FIFO queue until it is free (``--no-wait`` fails fast instead).
* A browser a human took control of is locked to the human. Resume only when the
  human tells you to ("keep going") -- then, and only then, pass ``--reclaim``.

Browsers are addressed by NAME (numbered ``browser-<N>`` for daemon-minted ones),
not a number. There is no default browser: run ``new`` first (it prints a name),
then drive that browser by its name.

Commands::

    agentic-browser-fleet ls [--include-tabs]
    agentic-browser-fleet new [name]            # prints the `playwright-cli attach --cdp=` line
    agentic-browser-fleet close <name>          # ends the browser; DELETES its profile
    agentic-browser-fleet acquire <name> [--reclaim] [--no-wait] [--max-wait S]
    agentic-browser-fleet release <name>        # alias: unlock
    agentic-browser-fleet handoff <name> "<reason>"   # alias: request-human

Driving is NOT here -- the agent attaches `playwright-cli` to the gated CDP endpoint this
CLI hands out. `playwright-cli --help` is the command reference.

The daemon address is discovered from ``data/.state/apps.toml`` (the same
registry ``layout.py`` reads), overridable via ``MINDS_BROWSER_SERVICE_URL``,
falling back to ``http://127.0.0.1:8081``. Browser panes are pulled into the
agent's view via ``system/scripts/layout.py`` (anchored at ``$BROWSER_FLEET_ANCHOR`` if
set -- a parent passes its chat address to sub-agents -- else the caller's own chat).
"""

import argparse
import json
import os
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from imbue.mngr.cli.output_helpers import write_human_line, write_stderr_line

_DEFAULT_URL = "http://127.0.0.1:8081"
_ENV_URL = "MINDS_BROWSER_SERVICE_URL"
_ENV_ANCHOR = "BROWSER_FLEET_ANCHOR"
_APPS_FILE = "data/.state/apps.toml"

# Exit codes the orchestrating agent can branch on.
_EXIT_OK = 0
_EXIT_ERROR = 1
_EXIT_PREEMPTED = 2  # a human took control mid-task
_EXIT_BUSY = 3  # held by a human (or another agent with --no-wait)
_EXIT_TIMEOUT = 4  # waited --max-wait and another agent still held it
_EXIT_USAGE = 64
_EXIT_NO_DAEMON = 69


def _out(message: str) -> None:
    write_human_line(message)


def _err(message: str) -> None:
    write_stderr_line(message)


def _repo_root() -> Path:
    """Walk up from cwd to the workspace root (where ``system/scripts/layout.py`` lives)."""
    here = Path.cwd()
    for candidate in (here, *here.parents):
        if (candidate / "system" / "scripts" / "layout.py").exists():
            return candidate
    return here


def _daemon_url() -> str:
    """Discover the browser daemon's base URL (env override, registry, then localhost)."""
    override = os.environ.get(_ENV_URL)
    if override:
        return override.rstrip("/")
    registry = Path(os.environ.get("MINDS_APPS_FILE", _APPS_FILE))
    if not registry.is_absolute():
        registry = _repo_root() / registry
    try:
        doc = tomllib.loads(registry.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return _DEFAULT_URL
    for app in doc.get("apps", []):
        if app.get("name") == "browser" and app.get("url"):
            return str(app["url"]).rstrip("/")
    return _DEFAULT_URL


def _agent_headers() -> dict[str, str]:
    """Identity headers; hard-fail if ``MNGR_AGENT_ID`` is unset (no null owner)."""
    agent_id = os.environ.get("MNGR_AGENT_ID")
    if not agent_id:
        _err("MNGR_AGENT_ID is not set -- run agentic-browser-fleet from inside an agent.")
        raise SystemExit(_EXIT_USAGE)
    headers = {"X-Mngr-Agent-Id": agent_id, "Content-Type": "application/json"}
    name = os.environ.get("MNGR_AGENT_NAME")
    if name:
        headers["X-Mngr-Agent-Name"] = name
    return headers


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """Single JSON request/response. Returns ``(status_code, parsed_body)``."""
    url = _daemon_url() + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_agent_headers())
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {"error": e.reason}
    except urllib.error.URLError as e:
        _err(f"cannot reach the browser daemon at {_daemon_url()} ({e.reason}). Is it running?")
        raise SystemExit(_EXIT_NO_DAEMON) from e


def _stream(path: str, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """POST and yield each line of the NDJSON response as it arrives.

    Closing the iterator (or a KeyboardInterrupt) closes the connection, which the
    daemon sees as a disconnect and releases the browser -- the connection is the lease.
    """
    url = _daemon_url() + path
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=_agent_headers())
    try:
        resp = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            payload = {"error": e.reason}
        yield {"type": "error", "text": payload.get("error", e.reason)}
        return
    except urllib.error.URLError as e:
        _err(f"cannot reach the browser daemon at {_daemon_url()} ({e.reason}). Is it running?")
        raise SystemExit(_EXIT_NO_DAEMON) from e
    with resp:
        for raw in resp:
            line = raw.decode().strip()
            if line:
                yield json.loads(line)


# --- pane pull-in (reuse system/scripts/layout.py) ----------------------------------


def _layout(*args: str, quiet: bool = False, no_wait: bool = False) -> bool:
    """Run ``system/scripts/layout.py`` with the given args from the repo root. True on success.
    ``quiet`` suppresses layout.py's raw stderr so the caller can substitute its own
    message (used by the pane-pull, which has a friendlier failure note). ``no_wait`` posts the
    op without waiting for it to settle in ``inspect`` -- the pane-pull is optimistic and a
    browser pane can take well over layout.py's 5s settle cap to register (its viewer must
    load), which otherwise reports a false failure even though the pane does open."""
    root = _repo_root()
    layout = root / "system" / "scripts" / "layout.py"
    if not layout.exists():
        return False
    env = os.environ.copy()
    if no_wait:
        env["MINDS_LAYOUT_NO_WAIT_STABLE"] = "1"
    result = subprocess.run(
        [sys.executable, str(layout), *args], cwd=str(root), capture_output=True, text=True, env=env
    )
    if result.returncode != 0 and not quiet:
        _err(result.stderr.strip() or f"layout {' '.join(args)} failed")
    return result.returncode == 0


def _resolve_active_view() -> tuple[bool, str | None]:
    """Resolve the view to surface a browser pane into, via ``layout.py context``.

    ``split`` needs a ``--view`` (mutating ops only apply on clients that have that view
    active), so the pane-pull must name the view the human is actually looking at.
    ``context`` is a read-only query over the client-activity log.

    Returns ``(reachable, view)``:
    * ``reachable`` is False when the shell can't be reached at all -- an isolated
      ``launch-task`` sub-agent in its own container, or no daemon. The caller skips
      silently: there is no screen of ours to surface into.
    * When reachable, ``view`` is the view to target -- the active view of the connected
      client that most recently messaged THIS agent (its chat instance is addressed by the
      agent id), else the most-recently-active connected client's view, else None
      (reachable but nothing to place it on).
    """
    root = _repo_root()
    script = root / "system" / "scripts" / "layout.py"
    if not script.exists():
        return (False, None)
    result = subprocess.run(
        [sys.executable, str(script), "context", "--json"], cwd=str(root), capture_output=True, text=True
    )
    if result.returncode != 0:
        return (False, None)  # unreachable (isolated sub-agent / no daemon)
    try:
        clients = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return (True, None)
    # ``context`` lists clients most-recently-active first; keep connected ones reporting a view.
    connected = [
        client
        for client in clients
        if isinstance(client, dict) and client.get("is_connected") and client.get("active_view")
    ]
    my_id = os.environ.get("MNGR_AGENT_ID")
    if my_id:
        my_address = f"app:chat?instance={my_id}"
        for client in connected:
            if any(msg.get("address") == my_address for msg in client.get("recent_messages", [])):
                return (True, str(client["active_view"]))
    if connected:
        return (True, str(connected[0]["active_view"]))
    return (True, None)


def _pull_in_pane(browser_name: str) -> None:
    """Surface browser ``browser_name`` as its OWN pane beside the requesting agent's chat,
    optimistically.

    Resolves the view the requester's client is looking at via ``layout.py context`` (see
    ``_resolve_active_view``). If the shell is unreachable -- an isolated ``launch-task``
    sub-agent in its own container -- we **skip silently**: there is no screen of ours to
    surface into. Otherwise we split the browser into that view next to the agent's own
    chat (``--relative-to self``), or the parent's chat when a parent handed a sub-agent
    its address via ``$BROWSER_FLEET_ANCHOR``. ``--new-group`` makes each browser its own
    pane; splitting an already-open one just focuses it, so this is safe to call repeatedly.

    If the split can't land (no target view, or the human isn't currently viewing it),
    we fall back to one neutral line offering the manual "+"-menu route -- the browser is
    up and fully drivable from the CLI either way; the pane is only a live-view convenience.
    """
    reachable, view = _resolve_active_view()
    if not reachable:
        return
    address = f"app:browser?instance={browser_name}"
    if view is not None:
        # A parent may hand a sub-agent its chat as an anchor; otherwise anchor on our own.
        anchor = os.environ.get(_ENV_ANCHOR)
        if anchor and _layout(
            "split", address, "--relative-to", anchor, "--direction", "right", "--new-group", "--view", view, quiet=True, no_wait=True
        ):
            return
        if _layout(
            "split", address, "--relative-to", "self", "--direction", "right", "--new-group", "--view", view, quiet=True, no_wait=True
        ):
            return
    # Reachable but couldn't place the pane. Not an error -- offer the manual route
    # without implying anything broke.
    _out(f"browser {browser_name} is ready. To watch it live, open it from the "
         '"+" menu (New browser -> ' + f"{browser_name}) in the side panel.")


# --- commands -----------------------------------------------------------------


def _owner_label(browser: dict[str, Any], me: str | None) -> str:
    if browser.get("crashed") or browser.get("lifecycle") == "crashed":
        return "crashed (gone -- start a new one)"
    if browser.get("lifecycle") == "init":
        return "starting (Chromium launching -- ready shortly)"
    if browser["controller"] == "agent":
        name = browser.get("owner_name") or browser.get("owner_agent_id") or "?"
        return "you" if browser.get("owner_agent_id") == me else f"agent {name}"
    return "human (took control)" if browser.get("human_pinned") else "free"


def cmd_ls(args: argparse.Namespace) -> int:
    status, payload = _request("GET", "/browsers")
    if status != 200:
        _err(payload.get("error", f"ls failed ({status})"))
        return _EXIT_ERROR
    browsers = payload.get("browsers", [])
    if not browsers:
        _out("no browsers yet -- use `new` to start one (it prints a name to drive by)")
        return _EXIT_OK
    me = os.environ.get("MNGR_AGENT_ID")
    for browser in browsers:
        tabs = browser.get("tabs", [])
        active = next((t for t in tabs if t.get("active")), None)
        where = (active.get("url") or active.get("title") or "") if active else "(no tab)"
        waiting = browser.get("waiting") or []
        queued = f"  [queued: {', '.join(waiting)}]" if waiting else ""
        _out(f"browser {browser['id']}: {_owner_label(browser, me)} -- {len(tabs)} tab(s), active: {where}{queued}")
        if getattr(args, "include_tabs", False):
            for tab in tabs:
                mark = "*" if tab.get("active") else " "
                _out(f"    [{tab.get('index')}]{mark} {tab.get('title') or ''}  {tab.get('url', '')}")
    return _EXIT_OK


def _attach_url(browser_name: str, *, attempts: int = 40, delay: float = 0.5) -> str:
    """Poll for the browser's attach URL.

    ``new`` returns as soon as the browser is REGISTERED; Chromium launches in the
    background and the capability token only exists once it is up, so the URL is not
    available synchronously.

    Only ``starting`` is worth waiting on. Any other answer -- a 404, a crashed browser,
    a daemon that says something unexpected -- will not become an attach URL by waiting,
    so bail immediately rather than burning the full timeout on a browser that is never
    going to come up.
    """
    for attempt in range(attempts):
        status, payload = _request("GET", f"/browsers/{browser_name}/attach")
        if status == 200 and payload.get("ok"):
            return str(payload.get("attach_url", ""))
        if status != 200 or payload.get("status") != "starting":
            return ""
        if attempt + 1 < attempts:
            time.sleep(delay)
    return ""


def _print_attach(browser_name: str) -> None:
    """Tell the agent how to drive this browser. This is the whole handover."""
    url = _attach_url(browser_name)
    if url:
        _out(f"   drive it:  playwright-cli -s={browser_name} attach --cdp={url}")
        # Point at the tool's own help rather than restating its commands anywhere: it
        # ships with the pinned version, so it cannot drift the way a copy would.
        _out(f"   then:      playwright-cli -s={browser_name} <command>   "
             f"(run `playwright-cli --help` for the commands)")
    else:
        _out(f"   (still starting up -- run `ls` in a few seconds, then attach to browser {browser_name})")


def cmd_new(args: argparse.Namespace) -> int:
    # `new` mints the first free `browser-<N>`; pass `new <name>` to choose one. A duplicate or invalid
    # name is rejected by the daemon (409 / 400) with a clear message.
    body: dict[str, Any] = {"name": args.name} if args.name else {}
    status, payload = _request("POST", "/browsers", body)
    if status == 200:
        # Surface the new browser's pane right away, so "open a new browser" visibly
        # opens one (idempotent with the pane-pull the first direct command also does).
        _pull_in_pane(payload["name"])
        _out(f"started browser {payload['name']}")
        _print_attach(payload["name"])
        return _EXIT_OK
    _err(payload.get("error", f"new failed ({status})"))
    if status == 400:  # invalid name -> a usage problem the agent can fix by picking another.
        return _EXIT_USAGE
    # 409 = duplicate name / fleet full, 503 = chromium installing -- both "try later".
    return _EXIT_BUSY if status in (409, 503) else _EXIT_ERROR


def cmd_close(args: argparse.Namespace) -> int:
    """Close an entire browser (all its tabs) and free its resources -- not just one tab.
    Use this when you're permanently done with a browser; its name is retired and its
    profile (cookies, logins) is deleted with it."""
    status, payload = _request("DELETE", f"/browsers/{args.name}")
    if status != 200:
        _err(payload.get("error", f"close failed ({status})"))
        return _EXIT_BUSY if status == 503 else _EXIT_ERROR  # 503 = fleet still restoring
    _out(f"closed browser {args.name}")
    return _EXIT_OK


def _render_event(event: dict[str, Any], browser_name: str) -> int | None:
    """Print one task/hold event; return an exit code for terminal events, else None."""
    kind = event.get("type")
    if kind == "waiting":
        busy = event.get("busy_name") or event.get("busy_agent_id") or "another agent"
        _out(f"browser {browser_name} is busy ({busy}) -- waiting for it to free up...")
    elif kind == "acquired":
        _out(f"(working on browser {browser_name})")
    elif kind == "held":
        _out(f"holding browser {browser_name} (Ctrl-C to release)")
    elif kind == "thinking":
        _out(f"[thinking] {event.get('text', '')}")
    elif kind == "action":
        _out(f"[action] {event.get('text', '')}")
    elif kind == "done":
        _out(f"done: {event.get('result', '')}")
        return _EXIT_OK
    elif kind == "error":
        _err(f"error: {event.get('text', '')}")
        return _EXIT_ERROR
    elif kind == "preempted":
        _out(
            f"lost control of browser {browser_name} (you took over). "
            'Send me a message ("keep going", "resume", whatever) when you want me to continue.'
        )
        return _EXIT_PREEMPTED
    elif kind == "busy_human":
        # task/lock acquire passes enqueue_on_busy=True, so a human pin enrols this agent in
        # the resume queue -- promise the wake (with the re-check fallback), like the direct path.
        _err(
            f"a human is controlling browser {browser_name}. You're queued to resume and will be messaged "
            f"when they hand it back (if you don't hear back in a while, re-run `ls` to "
            "check). Tell the user, then end your turn."
        )
        return _EXIT_BUSY
    elif kind == "busy_agent":
        _err(
            f"browser {browser_name} is held by another agent. You're queued for it and will be messaged "
            f"when it frees (if you don't hear back, re-run `ls` to check); for unrelated "
            "work, use a different browser (or `new`)."
        )
        return _EXIT_BUSY
    elif kind == "timed_out":
        _err(f"browser {browser_name} is still held by another agent after waiting; gave up.")
        return _EXIT_TIMEOUT
    elif kind == "starting":
        _err(f"browser {browser_name} is still starting up (Chromium is launching) -- "
             "try again in a few seconds.")
        return _EXIT_BUSY
    elif kind == "crashed":
        _err(f"browser {browser_name} crashed (Chromium was killed -- e.g. out of memory) and is gone. "
             f"Start a fresh one with `new` (it gets a new name).")
        return _EXIT_ERROR
    elif kind == "closed":
        _err(f"browser {browser_name} was closed and is gone. Start a fresh one with `new` (it gets a new name).")
        return _EXIT_ERROR
    elif kind == "lost_control":
        _out(
            f"lost control of browser {browser_name} (a human took over). You're queued to resume first; "
            f"I will be messaged when they hand it back (if you don't hear back in a while, re-run "
            f"`state {browser_name}` to check). Tell the user, then end your turn."
        )
        return _EXIT_PREEMPTED
    return None


def cmd_release(args: argparse.Namespace) -> int:
    status, payload = _request("POST", f"/browsers/{args.name}/release")
    if status != 200:
        _err(payload.get("error", f"release failed ({status})"))
        return _EXIT_ERROR
    _out(f"released browser {args.name}" if payload.get("released") else f"browser {args.name} was not yours to release")
    return _EXIT_OK


# --- direct control: you drive the browser yourself, one command at a time ----


def _render_action(payload: dict[str, Any], browser_name: str, kind: str) -> int:
    """Print one ownership-command result and return the exit code (owner-aware).

    The exit code is the whole point. Driving now happens over ``playwright-cli``, which
    exits 1 for ANY error -- a lease refusal and a stale ref are indistinguishable there.
    So ``acquire`` is what an agent branches on before it attaches, and this function is
    what gives it the differentiated code.
    """
    if payload.get("ok"):
        _out(f"ok: {kind}")
        return _EXIT_OK
    status = payload.get("status")
    # Only when the agent was actually enrolled to be woken (a state-CHANGING command, or an
    # explicit acquire/lock) do we tell it "you're queued ... messaged when it frees" and end
    # its turn (preempted). A read-only `state` peek on a busy browser enrols nothing -> say so
    # and let it move on (busy); never strand it on a resume message that won't come. And even
    # when enrolled, point at a `state` re-check fallback, since a wake can be lost (e.g. a
    # daemon restart drops the in-memory resume queue).
    enqueued = bool(payload.get("enqueued"))
    if status == "busy_human":
        if enqueued:
            _out(f"browser {browser_name}: the human took control -- you're queued to resume. They can "
                 "see you're waiting, and you'll be messaged to pick up when they hand it back (if you "
                 f"don't hear back in a while, re-run `ls` to check). Tell the user, then end your turn.")
            return _EXIT_PREEMPTED
        _out(f"a human is controlling browser {browser_name} right now (you were only looking, so you're not "
             f"queued for it). Use a different browser (or `new`), or re-run `ls` later to check.")
        return _EXIT_BUSY
    if status == "busy_agent":
        if enqueued:
            _out(f"browser {browser_name} is held by another agent -- you're queued for it and will be "
                 f"messaged when it frees (if you don't hear back in a while, re-run `ls` to "
                 f"check). For unrelated work, use a different browser (or `new`).")
        else:
            _out(f"another agent is using browser {browser_name} right now (you were only looking, so you're "
                 f"not queued). Use a different browser (or `new`), or re-run `ls` later to check.")
        return _EXIT_BUSY
    if status == "lost_control":
        if enqueued:
            _out(f"browser {browser_name}: the human took control mid-step -- you're queued to resume. "
                 "Tell the user you'll pick up when they hand it back (if you don't hear back in a while, "
                 f"re-run `ls` to check), then end your turn.")
            return _EXIT_PREEMPTED
        _out(f"a human took control of browser {browser_name} mid-step; you weren't queued. Use a different "
             f"browser, or re-run `ls` later to check.")
        return _EXIT_BUSY
    if status == "initializing":
        _err("the browser fleet is still starting up (restoring your saved browsers) -- "
             "try again in a few seconds. `ls` works now; this command needs the fleet ready.")
        return _EXIT_BUSY
    if status == "starting":
        _err(f"browser {browser_name} is still starting up (Chromium is launching) -- "
             "try again in a few seconds. It'll be ready shortly; just retry this command.")
        return _EXIT_BUSY
    if status == "crashed":
        _err(f"browser {browser_name} crashed (Chromium was killed -- e.g. out of memory) and is gone. "
             f"Start a fresh one with `new` (it gets a new name); browser {browser_name} won't come back.")
        return _EXIT_ERROR
    if status == "closed":
        _err(f"browser {browser_name} was closed and is gone. Start a fresh one with `new` (it gets a new name).")
        return _EXIT_ERROR
    if status == "stale_index":
        _err(payload.get("error") or f"that element index is stale -- run `state {browser_name}` again first")
        return _EXIT_ERROR
    if status == "timed_out":
        _err(f"browser {browser_name} stayed busy; gave up.")
        return _EXIT_TIMEOUT
    _err(payload.get("error") or f"command failed ({status})")
    return _EXIT_ERROR


def _action(browser_name: str, verb: str, kind: str, body: dict[str, Any] | None = None) -> int:
    status, payload = _request("POST", f"/browsers/{browser_name}/{verb}", body or {})
    if status == 404:
        _err(payload.get("error", f"no browser {browser_name}"))
        return _EXIT_ERROR
    # The first command for a browser (and the first after a human hands it back)
    # surfaces it as a pane split next to your chat, so the human can watch.
    if payload.get("newly_acquired"):
        _pull_in_pane(browser_name)
    return _render_action(payload, browser_name, kind)


def cmd_acquire(args: argparse.Namespace) -> int:
    _, payload = _request("POST", f"/browsers/{args.name}/acquire", {"reclaim": args.reclaim})
    if payload.get("ok"):
        _pull_in_pane(args.name)
        _out(f"acquired browser {args.name}")
        _print_attach(args.name)
        return _EXIT_OK
    return _render_action(payload, args.name, "acquire")


def cmd_handoff(args: argparse.Namespace) -> int:
    """Hand a browser to the human for a CAPTCHA / robot-check / login you can't do."""
    status, payload = _request("POST", f"/browsers/{args.name}/handoff", {"reason": args.reason})
    if status == 404:
        _err(payload.get("error", f"no browser {args.name}"))
        return _EXIT_ERROR
    if payload.get("ok"):
        _pull_in_pane(args.name)  # surface/focus the pane so the human sees what to solve
        _out(
            f"handed browser {args.name} to the human: {args.reason}. You're first in line to "
            f"resume. Tell the user what to do, end your turn, and re-run `state {args.name}` "
            "when they hand control back."
        )
        return _EXIT_PREEMPTED
    if payload.get("status") == "not_owner":
        _out(
            f"browser {args.name} isn't yours to hand off -- a human may already control it. "
            f"Run `state {args.name}` to see who has it; if the human took over, you're queued to resume."
        )
        return _EXIT_PREEMPTED
    return _render_action(payload, args.name, "handoff")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-browser-fleet", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_ls = sub.add_parser("ls", help="List active browsers, their owners, and their tabs.")
    p_ls.add_argument("--include-tabs", action="store_true", help="List every open tab per browser, not just the active one.")
    p_ls.set_defaults(func=cmd_ls)
    p_new = sub.add_parser("new", help="Start a new browser and print its name. Pass an optional name to choose one.")
    p_new.add_argument("name", nargs="?", default=None, help="Optional name (lowercase letters/digits/dashes, e.g. 'research-1'); a duplicate is rejected.")
    p_new.set_defaults(func=cmd_new)

    p_close = sub.add_parser("close", help="Close an entire browser (all tabs) and retire its name. For one tab, use `tab <name> close`.")
    p_close.add_argument("name")
    p_close.set_defaults(func=cmd_close)

    for verb in ("unlock", "release"):
        p_rel = sub.add_parser(verb, help="Release a browser you hold.")
        p_rel.add_argument("name")
        p_rel.set_defaults(func=cmd_release)

    p_acq = sub.add_parser("acquire", help="Reserve a browser across several commands (optional; the first command auto-acquires).")
    p_acq.add_argument("name")
    p_acq.add_argument("--reclaim", action="store_true", help="Take a browser back from a human -- ONLY when they told you to resume.")
    p_acq.set_defaults(func=cmd_acquire)

    for verb in ("handoff", "request-human"):
        p_handoff = sub.add_parser(
            verb,
            help="Hand a browser to the human for a CAPTCHA / robot-check / login you can't do, then stop.",
        )
        p_handoff.add_argument("name")
        p_handoff.add_argument("reason", nargs="?", default="human verification needed", help="What the human needs to do (e.g. 'solve the CAPTCHA').")
        p_handoff.set_defaults(func=cmd_handoff)

    return parser


def main() -> None:
    args = _build_parser().parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
