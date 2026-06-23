"""``agentic-browser-fleet``: drive the shared browser fleet from an agent's shell.

This is the CLI the Claude Code agent (the orchestrator) calls. It is a thin,
stateless HTTP client to the per-workspace browser daemon (runner.py). It does
NOT drive the browser itself: ``task`` hands a goal to a *browser-use* agent on
the chosen browser and streams that agent's Thinking/Action trace back here, to
the orchestrator's own output. ``take control`` is a human action in the UI, not
something this CLI does.

Ownership rules (enforced by the daemon, surfaced here):

* Each browser is controlled by exactly one party. ``task``/``lock`` acquire it;
  they release automatically when the command ends (the connection is the lease).
* Agents never preempt each other: ``task`` on a browser another agent holds
  waits in a FIFO queue until it is free (``--no-wait`` fails fast instead).
* A browser a human took control of is locked to the human. Resume only when the
  human tells you to ("keep going") -- then, and only then, pass ``--reclaim``.

Commands::

    agentic-browser-fleet ls
    agentic-browser-fleet new
    agentic-browser-fleet task <id> "<prompt>" [--reclaim] [--no-wait] [--max-wait S] [--no-pane]
    agentic-browser-fleet lock <id> [--no-wait] [--max-wait S]    # foreground hold; Ctrl-C releases
    agentic-browser-fleet unlock <id>                             # alias: release
    agentic-browser-fleet release <id>

The daemon address is discovered from ``runtime/applications.toml`` (the same
registry ``layout.py`` reads), overridable via ``MINDS_BROWSER_SERVICE_URL``,
falling back to ``http://127.0.0.1:8081``. Browser panes are pulled into the
agent's view via ``scripts/layout.py`` (anchored at ``$BROWSER_FLEET_ANCHOR`` if
set -- a parent passes its chat ref to sub-agents -- else the caller's own chat).
"""

import argparse
import json
import os
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from typing import Iterator

from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_stderr_line

_DEFAULT_URL = "http://127.0.0.1:8081"
_ENV_URL = "MINDS_BROWSER_SERVICE_URL"
_ENV_ANCHOR = "BROWSER_FLEET_ANCHOR"
_APPLICATIONS_FILE = "runtime/applications.toml"

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
    """Walk up from cwd to the workspace root (where ``scripts/layout.py`` lives)."""
    here = Path.cwd()
    for candidate in (here, *here.parents):
        if (candidate / "scripts" / "layout.py").exists():
            return candidate
    return here


def _daemon_url() -> str:
    """Discover the browser daemon's base URL (env override, registry, then localhost)."""
    override = os.environ.get(_ENV_URL)
    if override:
        return override.rstrip("/")
    registry = Path(os.environ.get("MINDS_APPLICATIONS_FILE", _APPLICATIONS_FILE))
    if not registry.is_absolute():
        registry = _repo_root() / registry
    try:
        doc = tomllib.loads(registry.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return _DEFAULT_URL
    for app in doc.get("applications", []):
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


# --- pane pull-in (reuse scripts/layout.py) ----------------------------------


def _layout(*args: str) -> bool:
    """Run ``scripts/layout.py`` with the given args from the repo root. True on success."""
    root = _repo_root()
    layout = root / "scripts" / "layout.py"
    if not layout.exists():
        return False
    result = subprocess.run(
        [sys.executable, str(layout), *args], cwd=str(root), capture_output=True, text=True
    )
    if result.returncode != 0:
        _err(result.stderr.strip() or f"layout {' '.join(args)} failed")
    return result.returncode == 0


def _pull_in_pane(browser_id: int) -> None:
    """Surface browser ``browser_id`` as a pane next to the controlling agent's chat.

    Anchor chain: ``$BROWSER_FLEET_ANCHOR`` (a parent passes its chat to sub-agents)
    -> the caller's own chat -> a new group. Best-effort: a layout failure never
    fails the task (the browser still runs), it just warns.
    """
    ref = f"service:browser?session={browser_id}"
    anchor = os.environ.get(_ENV_ANCHOR)
    if anchor and _layout("split", ref, "--relative-to", anchor, "--direction", "right"):
        return
    if anchor:
        _err(f"anchor {anchor!r} not found; opening browser {browser_id} by my own chat instead")
    if not _layout("open", ref):
        _err(f"could not pull browser {browser_id} into view (it is still running headless)")


# --- commands -----------------------------------------------------------------


def _owner_label(browser: dict[str, Any], me: str | None) -> str:
    if browser["controller"] == "agent":
        name = browser.get("owner_name") or browser.get("owner_agent_id") or "?"
        return "you" if browser.get("owner_agent_id") == me else f"agent {name}"
    return "human (took control)" if browser.get("human_pinned") else "free"


def cmd_ls(_args: argparse.Namespace) -> int:
    status, payload = _request("GET", "/browsers")
    if status != 200:
        _err(payload.get("error", f"ls failed ({status})"))
        return _EXIT_ERROR
    browsers = payload.get("browsers", [])
    if not browsers:
        _out("no browsers yet (use `new`, or `task 0` to start the default browser)")
        return _EXIT_OK
    me = os.environ.get("MNGR_AGENT_ID")
    for browser in browsers:
        active = next((t for t in browser.get("tabs", []) if t.get("active")), None)
        where = (active.get("url") or active.get("title") or "") if active else "(no tab)"
        ntabs = len(browser.get("tabs", []))
        _out(f"browser {browser['id']}: {_owner_label(browser, me)} -- {ntabs} tab(s), active: {where}")
    return _EXIT_OK


def cmd_new(_args: argparse.Namespace) -> int:
    status, payload = _request("POST", "/browsers")
    if status == 200:
        _out(f"started browser {payload['id']}")
        return _EXIT_OK
    _err(payload.get("error", f"new failed ({status})"))
    return _EXIT_BUSY if status == 409 else _EXIT_ERROR


def _render_event(event: dict[str, Any], browser_id: int) -> int | None:
    """Print one task/hold event; return an exit code for terminal events, else None."""
    kind = event.get("type")
    if kind == "waiting":
        busy = event.get("busy_name") or event.get("busy_agent_id") or "another agent"
        _out(f"browser {browser_id} is busy ({busy}) -- waiting for it to free up...")
    elif kind == "acquired":
        _out(f"(working on browser {browser_id})")
    elif kind == "held":
        _out(f"holding browser {browser_id} (Ctrl-C to release)")
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
            f"lost control of browser {browser_id} (you took over). "
            'Send me a message ("keep going", "resume", whatever) when you want me to continue.'
        )
        return _EXIT_PREEMPTED
    elif kind == "busy_human":
        _err(
            f"browser {browser_id} is under human control. It is yours to drive; when you are done, "
            'click "Return to agents" (or tell me to resume and I will reclaim it).'
        )
        return _EXIT_BUSY
    elif kind == "busy_agent":
        _err(f"browser {browser_id} is held by another agent (use without --no-wait to queue for it).")
        return _EXIT_BUSY
    elif kind == "timed_out":
        _err(f"browser {browser_id} is still held by another agent after waiting; gave up.")
        return _EXIT_TIMEOUT
    return None


def cmd_task(args: argparse.Namespace) -> int:
    if not args.no_pane:
        _pull_in_pane(args.id)
    body: dict[str, Any] = {"prompt": args.prompt, "reclaim": args.reclaim, "wait": not args.no_wait}
    if args.max_wait is not None:
        body["max_wait"] = args.max_wait
    exit_code = _EXIT_ERROR
    try:
        for event in _stream(f"/browsers/{args.id}/task", body):
            code = _render_event(event, args.id)
            if code is not None:
                exit_code = code
    except KeyboardInterrupt:
        _err("interrupted -- released the browser.")
        return _EXIT_OK
    return exit_code


def cmd_lock(args: argparse.Namespace) -> int:
    if not args.no_pane:
        _pull_in_pane(args.id)
    body: dict[str, Any] = {"wait": not args.no_wait}
    if args.max_wait is not None:
        body["max_wait"] = args.max_wait
    try:
        for event in _stream(f"/browsers/{args.id}/hold", body):
            code = _render_event(event, args.id)
            if code is not None and event.get("type") != "held":
                return code
    except KeyboardInterrupt:
        _err("released the browser.")
        return _EXIT_OK
    return _EXIT_OK


def cmd_release(args: argparse.Namespace) -> int:
    status, payload = _request("POST", f"/browsers/{args.id}/release")
    if status != 200:
        _err(payload.get("error", f"release failed ({status})"))
        return _EXIT_ERROR
    _out(f"released browser {args.id}" if payload.get("released") else f"browser {args.id} was not yours to release")
    return _EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-browser-fleet", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ls", help="List active browsers, their owners, and their tabs.").set_defaults(func=cmd_ls)
    sub.add_parser("new", help="Start a new browser and print its id.").set_defaults(func=cmd_new)

    p_task = sub.add_parser("task", help="Run a browser-use task on a browser; stream its trace.")
    p_task.add_argument("id", type=int, help="Browser id (0 is the default browser).")
    p_task.add_argument("prompt", help="The high-level goal for the browser-use agent.")
    p_task.add_argument("--reclaim", action="store_true", help="Resume a browser a human took control of -- ONLY when the human told you to.")
    p_task.add_argument("--no-wait", action="store_true", help="Fail fast if another agent holds it, instead of queueing.")
    p_task.add_argument("--max-wait", type=float, default=None, help="Seconds to wait for another agent to release before giving up.")
    p_task.add_argument("--no-pane", action="store_true", help="Do not pull the browser into a UI pane.")
    p_task.set_defaults(func=cmd_task)

    p_lock = sub.add_parser("lock", help="Hold a browser (foreground) until Ctrl-C; releases on exit.")
    p_lock.add_argument("id", type=int)
    p_lock.add_argument("--no-wait", action="store_true")
    p_lock.add_argument("--max-wait", type=float, default=None)
    p_lock.add_argument("--no-pane", action="store_true")
    p_lock.set_defaults(func=cmd_lock)

    for verb in ("unlock", "release"):
        p_rel = sub.add_parser(verb, help="Release a browser you hold.")
        p_rel.add_argument("id", type=int)
        p_rel.set_defaults(func=cmd_release)

    return parser


def main() -> None:
    args = _build_parser().parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
