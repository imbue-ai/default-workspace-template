#!/usr/bin/env python3
"""Fast smoke-test and screenshot capture for local workspace apps.

Performs active socket/HTTP readiness polling, reload verification, and
optional headless Playwright browser screenshot capture with optimized flags.

Usage:
    python3 system/scripts/smoketest_app.py <name-or-port>
    python3 system/scripts/smoketest_app.py <name-or-port> --marker "Add a task"
    python3 system/scripts/smoketest_app.py <name-or-port> --screenshot /tmp/mock.png
    python3 system/scripts/smoketest_app.py <name-or-port> --marker "Add a task" --screenshot /tmp/mock.png
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

LOCALHOST_PORT_RE = re.compile(r"http://(?:localhost|127\.0\.0\.1):(\d+)")
SUPERVISOR_URL_RE = re.compile(r"--url\s+http://(?:localhost|127\.0\.0\.1):(\d+)")
PORT_ASSIGN_RE = re.compile(r"PORT\s*=\s*(?:int\(.*,\s*)?(\d+)")
DEFAULT_FORTRESS_PATH = Path("/opt/fortress/tilion-fortress/tilion")


def _find_repo_root() -> Path:
    script_root = Path(__file__).resolve().parents[2]
    if (script_root / "pyproject.toml").exists() and (script_root / "system/supervisord.conf").exists():
        return script_root
    current = Path.cwd().resolve()
    for parent in [current, *current.parents]:
        if (parent / "pyproject.toml").exists() and (parent / "system/supervisord.conf").exists():
            return parent
    return current


def _resolve_target(target: str, repo_root: Path) -> tuple[int, Optional[str], Optional[Path]]:
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
            section = text[section_start : next_section if next_section != -1 else len(text)]
            m = SUPERVISOR_URL_RE.search(section)
            if m:
                return int(m.group(1)), name, runner_path

    # Check runner.py default PORT
    if runner_path and runner_path.exists():
        content = runner_path.read_text()
        m = PORT_ASSIGN_RE.search(content)
        if m:
            return int(m.group(1)), name, runner_path

    sys.exit(f"error: could not resolve port for app '{target}' (checked apps.toml, supervisord.conf, runner.py)")


def _poll_health_for_reload(port: int, runner_path: Optional[Path], timeout: float, interval: float) -> None:
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
            req = urllib.request.Request(health_url, headers={"User-Agent": "smoketest"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    body = resp.read().decode("utf-8", errors="replace")
                    try:
                        data = json.loads(body)
                        started_at = data.get("started_at")
                        if started_at is not None and float(started_at) >= (mtime - 0.1):
                            return
                    except Exception:
                        return
        except Exception:
            pass
        time.sleep(interval)


def _poll_http(url: str, marker: Optional[str], timeout: float, interval: float) -> tuple[int, str]:
    """Actively polls the target URL until 200 OK and optional marker is found."""
    deadline = time.time() + timeout
    last_error = None
    last_body = ""
    last_code = 0

    while time.time() < deadline:
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

    sys.exit(f"error: failed to connect to {url} within {timeout}s (last error: {last_error})")


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
        # Re-exec with repo root .venv python if playwright is not in current environment
        repo_root = _find_repo_root()
        venv_py = repo_root / ".venv/bin/python"
        if venv_py.exists() and sys.executable != str(venv_py):
            os.execv(str(venv_py), [str(venv_py), *sys.argv])
        sys.exit("error: playwright is not installed. Run with `uv run python ...` or activate workspace .venv.")

    executable_path = str(DEFAULT_FORTRESS_PATH) if DEFAULT_FORTRESS_PATH.exists() else None

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
            sys.exit(f"error: browser loaded page, but marker {marker!r} was missing in DOM.\nSnippet:\n{snippet}")

        if screenshot_path:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path))
            print(f"Screenshot: {screenshot_path}")

        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("target", help="app name (e.g. todo), port (e.g. 8080), or full URL")
    parser.add_argument("--marker", default=None, help="expected string in the response or DOM")
    parser.add_argument("--screenshot", default=None, help="path to write page screenshot (PNG)")
    parser.add_argument("--path", default="/", help="URL path to test (defaults to '/')")
    parser.add_argument("--timeout", type=float, default=5.0, help="timeout in seconds (default: 5.0)")
    parser.add_argument("--poll-interval", type=float, default=0.05, help="retry interval in seconds (default: 0.05)")
    parser.add_argument("--wait-reload", action="store_true", help="verify /health reload timestamp against runner mtime")
    args = parser.parse_args()

    repo_root = _find_repo_root()
    port, app_name, runner_path = _resolve_target(args.target, repo_root)

    t0 = time.time()

    # 1. Reload verification if applicable
    if args.wait_reload or runner_path:
        _poll_health_for_reload(port, runner_path, timeout=min(args.timeout, 3.0), interval=args.poll_interval)

    # 2. HTTP polling & marker check
    url = f"http://127.0.0.1:{port}{args.path}"
    status_code, body = _poll_http(url, args.marker, timeout=args.timeout, interval=args.poll_interval)
    t_http = time.time()

    screenshot_path = Path(args.screenshot).resolve() if args.screenshot else None

    # 3. Playwright render & screenshot if requested
    if screenshot_path:
        _run_playwright_check(url, args.marker, screenshot_path, timeout=args.timeout)
        t_shot = time.time()
        print(f"OK: {url} responding (HTTP {status_code}) in {t_http - t0:.2f}s (full render + screenshot in {t_shot - t0:.2f}s)")
    else:
        print(f"OK: {url} responding (HTTP {status_code}) in {t_http - t0:.2f}s")

    if args.marker:
        print(f"Marker confirmed: {args.marker!r}")


if __name__ == "__main__":
    main()
