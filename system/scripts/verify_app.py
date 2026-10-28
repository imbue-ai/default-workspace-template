#!/usr/bin/env python3
"""Restart, smoke-test, screenshot and surface a local workspace app in one call.

Performs active socket/HTTP readiness polling, reload verification, and
optional headless Playwright browser screenshot capture with optimized flags.
With ``--restart`` it first bounces the app's supervisord program without
waiting out its ``startsecs`` window: supervisord is asked to stop the program
and to start it again *without* waiting, and readiness is then read from the
port itself (usually well under a second) while the program's state is watched
so a crash on start fails the call at once with the stderr log. ``--open`` and
``--refresh`` surface the verified page to the user through ``workspace-layout``.

Usage:
    python3 system/scripts/verify_app.py <name-or-port>
    python3 system/scripts/verify_app.py <name-or-port> --marker "Add a task"
    python3 system/scripts/verify_app.py <name-or-port> --screenshot /tmp/mock.png
    python3 system/scripts/verify_app.py <name> --restart --marker "Add a task" --screenshot /tmp/mock.png --open
    python3 system/scripts/verify_app.py <name> --restart --marker "Add a task" --refresh
"""

import argparse
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xmlrpc.client
from collections.abc import Callable
from pathlib import Path
from typing import Optional

LOCALHOST_PORT_RE = re.compile(r"http://(?:localhost|127\.0\.0\.1):(\d+)")
SUPERVISOR_URL_RE = re.compile(r"--url\s+http://(?:localhost|127\.0\.0\.1):(\d+)")
PORT_ASSIGN_RE = re.compile(r"PORT\s*=\s*(?:int\(.*,\s*)?(\d+)")
DEFAULT_FORTRESS_PATH = Path("/opt/fortress/tilion-fortress/tilion")
# The socket supervisord's [unix_http_server] binds (system/supervisord.conf);
# the env var names another one, as it does for the shell app's liveness probes.
SUPERVISOR_SOCKET = "/var/run/supervisor.sock"
SUPERVISOR_SOCKET_ENV = "MINDS_SUPERVISOR_SOCKET"
SUPERVISOR_LOG_DIR = Path("/var/log/supervisor")
# A program in one of these states is (still) running as far as supervisord's
# stopProcess is concerned; a start on it is refused as ALREADY_STARTED.
_STOPPABLE_STATES = frozenset({"RUNNING", "STARTING", "BACKOFF"})
# After a start, any of these means the program died (or never ran) instead
# of settling: supervisord reports BACKOFF within a tenth of a second of a
# crash, long before the startsecs window would have made supervisorctl
# return.
_DEAD_AFTER_START_STATES = frozenset(
    {"BACKOFF", "FATAL", "EXITED", "STOPPED", "UNKNOWN"}
)
# supervisord's fault codes (supervisor.xmlrpc.Faults) for the two races a
# restart tolerates.
_FAULT_ALREADY_STARTED = 60
_FAULT_NOT_RUNNING = 70
# --restart needs room for an interpreter to import the app and bind; the
# plain check keeps the short default.
RESTART_MIN_TIMEOUT = 15.0


def _find_repo_root() -> Path:
    script_root = Path(__file__).resolve().parents[2]
    if (script_root / "pyproject.toml").exists() and (
        script_root / "system/supervisord.conf"
    ).exists():
        return script_root
    current = Path.cwd().resolve()
    for parent in [current, *current.parents]:
        if (parent / "pyproject.toml").exists() and (
            parent / "system/supervisord.conf"
        ).exists():
            return parent
    return current


def _resolve_target(
    target: str, repo_root: Path
) -> tuple[int, Optional[str], Optional[Path]]:
    """Resolves target (name, port, or URL) to (port, app_name, runner_path)."""
    # 1. URL input
    if target.startswith("http://") or target.startswith("https://"):
        parsed = urllib.parse.urlparse(target)
        if parsed.port:
            return parsed.port, None, None
        return (443 if parsed.scheme == "https" else 80), None, None

    # 2. Port number input
    if target.isdigit():
        return int(target), None, None

    # 3. App name
    name = target
    package = name.replace("-", "_")
    runner_path = repo_root / f"system/apps/{package}/src/{package}/runner.py"
    if not runner_path.exists():
        runner_path = None

    # Check data/.state/apps.toml
    apps_toml = repo_root / "data/.state/apps.toml"
    if apps_toml.exists():
        try:
            import tomllib

            data = tomllib.loads(apps_toml.read_text())
            for app_entry in data.get("apps", []):
                if app_entry.get("name") == name and "url" in app_entry:
                    m = LOCALHOST_PORT_RE.search(app_entry["url"])
                    if m:
                        return int(m.group(1)), name, runner_path
        except Exception:
            pass

    # Check system/supervisord.conf
    sup_conf = repo_root / "system/supervisord.conf"
    if sup_conf.exists():
        text = sup_conf.read_text()
        section_start = text.find(f"[program:{name}]")
        if section_start != -1:
            next_section = text.find("[program:", section_start + 1)
            section = text[
                section_start : next_section if next_section != -1 else len(text)
            ]
            m = SUPERVISOR_URL_RE.search(section)
            if m:
                return int(m.group(1)), name, runner_path

    # Check runner.py default PORT
    if runner_path and runner_path.exists():
        content = runner_path.read_text()
        m = PORT_ASSIGN_RE.search(content)
        if m:
            return int(m.group(1)), name, runner_path

    sys.exit(
        f"error: could not resolve port for app '{target}' (checked apps.toml, supervisord.conf, runner.py)"
    )


class _UnixSocketTransport(xmlrpc.client.Transport):
    """XML-RPC over supervisord's unix socket (the stdlib transport only speaks TCP)."""

    def __init__(self, socket_path: str) -> None:
        super().__init__()
        self._socket_path = socket_path

    def make_connection(
        self, host: str | tuple[str, dict[str, str]]
    ) -> http.client.HTTPConnection:
        socket_path = self._socket_path

        class _Connection(http.client.HTTPConnection):
            def connect(self) -> None:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(socket_path)

        return _Connection("localhost")


def supervisor_socket_path() -> str:
    """The socket to reach supervisord on: the env override when set, else the default."""
    return os.environ.get(SUPERVISOR_SOCKET_ENV) or SUPERVISOR_SOCKET


def _supervisor(socket_path: str):
    """The ``supervisor`` namespace of supervisord's RPC interface."""
    proxy = xmlrpc.client.ServerProxy(
        "http://localhost/RPC2", transport=_UnixSocketTransport(socket_path)
    )
    return proxy.supervisor


def needs_stop(statename: str) -> bool:
    """True when supervisord must stop the program before it can be started again."""
    return statename in _STOPPABLE_STATES


def is_dead_after_start(statename: str) -> bool:
    """True when a program started a moment ago has already died or given up."""
    return statename in _DEAD_AFTER_START_STATES


def effective_timeout(timeout: float, restart: bool) -> float:
    return max(timeout, RESTART_MIN_TIMEOUT) if restart else timeout


def _stderr_tail(name: str, lines: int = 20) -> str:
    log_path = SUPERVISOR_LOG_DIR / f"{name}-stderr.log"
    try:
        tail = log_path.read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return f"(no stderr log at {log_path})"
    return f"--- last {len(tail)} lines of {log_path} ---\n" + "\n".join(tail)


def _restart_program(sup, name: str) -> Callable[[], Optional[str]]:
    """Bounce ``name`` without waiting out ``startsecs``; return a check that reports an early death.

    supervisorctl's ``restart`` blocks until the program has been up ``startsecs``
    (30 s for a scaffolded app), although the port answers within a second. The
    RPC start with ``wait=False`` returns at once, and the caller reads readiness
    from the port instead; the returned callable is polled meanwhile so a program
    that dies on start fails the call immediately, stderr log attached.
    """
    try:
        state = sup.getProcessInfo(name)["statename"]
    except OSError as error:
        sys.exit(
            f"error: cannot reach supervisord at {supervisor_socket_path()} ({error}); is it running?"
        )
    except xmlrpc.client.Fault as fault:
        sys.exit(
            f"error: supervisord knows no program {name!r} ({fault.faultString}); is its .conf loaded (supervisorctl reread && supervisorctl update)?"
        )
    if needs_stop(state):
        try:
            sup.stopProcess(name, True)
        except xmlrpc.client.Fault as fault:
            # It exited on its own between the state read and the stop.
            if fault.faultCode != _FAULT_NOT_RUNNING:
                sys.exit(
                    f"error: supervisord could not stop {name}: {fault.faultString}"
                )
        except OSError as error:
            sys.exit(
                f"error: lost supervisord at {supervisor_socket_path()} while stopping {name} ({error})"
            )
    try:
        sup.startProcess(name, False)
    except xmlrpc.client.Fault as fault:
        # ALREADY_STARTED: autorestart brought it back meanwhile, which is the
        # restart this call wanted. Anything else (SPAWN_ERROR,
        # ABNORMAL_TERMINATION, ...) is a start that failed at once.
        if fault.faultCode != _FAULT_ALREADY_STARTED:
            sys.exit(
                f"error: supervisord could not start {name}: {fault.faultString}\n{_stderr_tail(name)}"
            )
    except OSError as error:
        sys.exit(
            f"error: lost supervisord at {supervisor_socket_path()} while starting {name} ({error})"
        )

    def early_death() -> Optional[str]:
        try:
            statename = sup.getProcessInfo(name)["statename"]
        except (OSError, xmlrpc.client.Fault) as error:
            return f"lost track of {name} in supervisord after the restart: {error}"
        if is_dead_after_start(statename):
            return f"{name} is {statename} after the restart: it died before answering.\n{_stderr_tail(name)}"
        return None

    return early_death


def _surface(repo_root: Path, name: str, mode: str) -> None:
    """Open the app's window beside the chat, or refresh its open windows, through workspace-layout."""
    args = ["open", name, "--beside"] if mode == "open" else ["refresh", "--app", name]
    done = subprocess.run(
        ["uv", "run", "--no-sync", "workspace-layout", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    output = (done.stdout + done.stderr).strip()
    if done.returncode != 0:
        sys.exit(
            f"error: workspace-layout {' '.join(args)} exited {done.returncode}:\n{output}"
        )
    print(f"workspace-layout {mode}: {output or 'ok'}")


def _poll_health_for_reload(
    port: int, runner_path: Optional[Path], timeout: float, interval: float
) -> None:
    """If runner_path was recently modified, ensure the server has reloaded with started_at >= mtime."""
    if not runner_path or not runner_path.exists():
        return

    mtime = runner_path.stat().st_mtime
    now = time.time()
    # If the file was modified in the last 15 seconds, verify reload
    if (now - mtime) > 15.0:
        return

    health_url = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                health_url, headers={"User-Agent": "smoketest"}
            )
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    body = resp.read().decode("utf-8", errors="replace")
                    try:
                        data = json.loads(body)
                        started_at = data.get("started_at")
                        if started_at is not None and float(started_at) >= (
                            mtime - 0.1
                        ):
                            return
                    except Exception:
                        return
        except Exception:
            pass
        time.sleep(interval)


def _poll_http(
    url: str,
    marker: Optional[str],
    timeout: float,
    interval: float,
    abort_check: Optional[Callable[[], Optional[str]]] = None,
) -> tuple[int, str]:
    """Actively polls the target URL until 200 OK and optional marker is found.

    ``abort_check`` is asked between attempts; a message from it ends the poll
    with that error instead of waiting for the deadline.
    """
    deadline = time.time() + timeout
    last_error = None
    last_body = ""
    last_code = 0

    while time.time() < deadline:
        if abort_check is not None:
            failure = abort_check()
            if failure is not None:
                sys.exit(f"error: {failure}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "smoketest"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                last_code = resp.status
                last_body = resp.read().decode("utf-8", errors="replace")
                if 200 <= last_code < 400:
                    if marker is None or marker in last_body:
                        return last_code, last_body
        except urllib.error.HTTPError as e:
            last_code = e.code
            try:
                last_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                last_body = ""
            last_error = f"HTTP {e.code}"
        except Exception as e:
            last_error = str(e)

        time.sleep(interval)

    if marker and (200 <= last_code < 400) and marker not in last_body:
        snippet = (last_body[:300] + "...") if len(last_body) > 300 else last_body
        sys.exit(
            f"error: HTTP responded {last_code} within {timeout}s, but marker {marker!r} was not found.\n"
            f"Response snippet:\n{snippet}"
        )

    sys.exit(
        f"error: failed to connect to {url} within {timeout}s (last error: {last_error})"
    )


def _reexec_under_venv_without_playwright(repo_root: Path) -> None:
    """Re-run this script under the workspace venv when the current interpreter lacks Playwright.

    Called before any side effect: the re-exec repeats ``main()`` from the top,
    so doing it after ``--restart`` would bounce the program a second time.
    """
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        venv_py = repo_root / ".venv/bin/python"
        if venv_py.exists() and sys.executable != str(venv_py):
            os.execv(str(venv_py), [str(venv_py), *sys.argv])


def _run_playwright_check(
    url: str,
    marker: Optional[str],
    screenshot_path: Optional[Path],
    timeout: float,
) -> None:
    """Run headless Playwright check with fast launch flags and domcontentloaded."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "error: playwright is not installed. Run with `uv run python ...` or activate workspace .venv."
        )

    executable_path = (
        str(DEFAULT_FORTRESS_PATH) if DEFAULT_FORTRESS_PATH.exists() else None
    )

    with sync_playwright() as p:
        launch_kwargs = {
            "args": [
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--single-process",
            ],
            "headless": True,
        }
        if executable_path:
            launch_kwargs["executable_path"] = executable_path

        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))

        content = page.content()
        if marker and marker not in content:
            snippet = (content[:300] + "...") if len(content) > 300 else content
            browser.close()
            sys.exit(
                f"error: browser loaded page, but marker {marker!r} was missing in DOM.\nSnippet:\n{snippet}"
            )

        if screenshot_path:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path))
            print(f"Screenshot: {screenshot_path}")

        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument(
        "target", help="app name (e.g. todo), port (e.g. 8080), or full URL"
    )
    parser.add_argument(
        "--marker", default=None, help="expected string in the response or DOM"
    )
    parser.add_argument(
        "--screenshot", default=None, help="path to write page screenshot (PNG)"
    )
    parser.add_argument(
        "--path", default="/", help="URL path to test (defaults to '/')"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help=f"timeout in seconds (default: 5.0; at least {RESTART_MIN_TIMEOUT:g} with --restart)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.05,
        help="retry interval in seconds (default: 0.05)",
    )
    parser.add_argument(
        "--wait-reload",
        action="store_true",
        help="verify /health reload timestamp against runner mtime",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="restart the app's supervisord program first, without waiting out its startsecs window (app name only)",
    )
    surface = parser.add_mutually_exclusive_group()
    surface.add_argument(
        "--open",
        action="store_true",
        help="after the checks pass, open the app's window beside the chat",
    )
    surface.add_argument(
        "--refresh",
        action="store_true",
        help="after the checks pass, reload the app's open windows",
    )
    args = parser.parse_args()

    repo_root = _find_repo_root()
    port, app_name, runner_path = _resolve_target(args.target, repo_root)
    if (args.restart or args.open or args.refresh) and app_name is None:
        sys.exit(
            "error: --restart, --open and --refresh need the app's name, not a port or URL"
        )
    if args.screenshot:
        _reexec_under_venv_without_playwright(repo_root)
    timeout = effective_timeout(args.timeout, args.restart)

    t0 = time.time()

    # 0. Restart without the startsecs wait; the port poll below is the readiness check.
    abort_check = None
    if args.restart:
        abort_check = _restart_program(_supervisor(supervisor_socket_path()), app_name)

    # 1. Reload verification if applicable (a fresh restart needs none)
    if (args.wait_reload or runner_path) and not args.restart:
        _poll_health_for_reload(
            port, runner_path, timeout=min(timeout, 3.0), interval=args.poll_interval
        )

    # 2. HTTP polling & marker check
    url = f"http://127.0.0.1:{port}{args.path}"
    status_code, body = _poll_http(
        url,
        args.marker,
        timeout=timeout,
        interval=args.poll_interval,
        abort_check=abort_check,
    )
    t_http = time.time()
    if abort_check is not None:
        failure = abort_check()
        if failure is not None:
            sys.exit(f"error: {failure}")

    screenshot_path = Path(args.screenshot).resolve() if args.screenshot else None

    # 3. Playwright render & screenshot if requested
    restarted = "restarted and " if args.restart else ""
    if screenshot_path:
        _run_playwright_check(url, args.marker, screenshot_path, timeout=timeout)
        t_shot = time.time()
        print(
            f"OK: {url} {restarted}responding (HTTP {status_code}) in {t_http - t0:.2f}s (full render + screenshot in {t_shot - t0:.2f}s)"
        )
    else:
        print(
            f"OK: {url} {restarted}responding (HTTP {status_code}) in {t_http - t0:.2f}s"
        )

    if args.marker:
        print(f"Marker confirmed: {args.marker!r}")

    # 4. Surface the verified page
    if args.open or args.refresh:
        _surface(repo_root, app_name, "open" if args.open else "refresh")


if __name__ == "__main__":
    main()
