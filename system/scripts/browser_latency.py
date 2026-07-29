#!/usr/bin/env python3
"""Live-view latency readout: how close is the browser stream to the network floor?

    python3 system/scripts/browser_latency.py [name] [--watch]

Three numbers per browser, gathered by the viewer page and aggregated by the
browser daemon (see GET /browsers/<name>/latency):

  rtt            a periodic timestamp echo over the cast socket -- the same
                 network path the video rides. This is the floor.
  click>photon   on a real click in the live view, the time until a pixel near
                 the click visibly changes. This is what you feel. Clicks that
                 change nothing on screen produce no sample.
  overhead       click>photon minus rtt: the pipeline's own cost above the
                 network. Healthy is roughly frame interval + encode + decode,
                 ~30-80ms.

Standalone and stdlib-only on purpose: this is a HUMAN diagnostic, so it must
run from any shell -- no venv, no MNGR_AGENT_ID (that requirement exists for the
fleet CLI's ownership verbs, and a read-only readout has no owner). With no name
it reports every browser in the fleet. Samples only exist while a viewer pane is
open: the viewer is the one vantage point that can see both ends of the wire.
"""

import argparse
import json
import os
import sys
import threading
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_DEFAULT_URL = "http://127.0.0.1:8081"
_APPS_FILE = "data/.state/apps.toml"


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        if (candidate / "system" / "scripts" / "forward_port.py").exists():
            return candidate
    return Path.cwd()


def _daemon_url() -> str:
    override = os.environ.get("MINDS_BROWSER_SERVICE_URL")
    if override:
        return override.rstrip("/")
    registry = _repo_root() / _APPS_FILE
    try:
        doc = tomllib.loads(registry.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return _DEFAULT_URL
    for app in doc.get("apps", []):
        if app.get("name") == "browser" and app.get("url"):
            return str(app["url"]).rstrip("/")
    return _DEFAULT_URL


def _get(path: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(_daemon_url() + path, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {"error": e.reason}
    except urllib.error.URLError as e:
        print(f"cannot reach the browser daemon at {_daemon_url()} ({e.reason}). Is it running?", file=sys.stderr)
        raise SystemExit(69) from e


def _browser_names() -> list[str]:
    status, payload = _get("/browsers")
    if status != 200:
        print(payload.get("error", f"listing browsers failed ({status})"), file=sys.stderr)
        raise SystemExit(1)
    return [b["id"] for b in payload.get("browsers", [])]


def _render(name: str, payload: dict[str, Any]) -> None:
    rtt = payload.get("rtt")
    click = payload.get("click_photon")
    overhead = payload.get("overhead_ms")
    print(f"browser {name}")
    if rtt is None:
        print("  rtt           -- no samples yet; open this browser's pane and leave it open a few seconds")
    else:
        print(
            f"  rtt           p50 {rtt['p50_ms']:7.1f}ms   p95 {rtt['p95_ms']:7.1f}ms"
            f"   (n={rtt['n']})   spikes {rtt.get('spikes_recent', 0)}/{rtt.get('spike_window', 0)}"
        )
    if click is None:
        print("  click>photon  -- no samples yet; click inside the live view")
    else:
        print(f"  click>photon  p50 {click['p50_ms']:7.1f}ms   p95 {click['p95_ms']:7.1f}ms   (n={click['n']})")
    if overhead is not None:
        verdict = "at the RTT floor" if overhead <= 80 else "pipeline overhead worth investigating"
        print(f"  overhead      {overhead:7.1f}ms  (click>photon minus rtt) -- {verdict}; healthy ~30-80ms")


def main() -> int:
    parser = argparse.ArgumentParser(description="Show live-view latency for one or all browsers.")
    parser.add_argument("name", nargs="?", default=None, help="Browser name; omit to show the whole fleet.")
    parser.add_argument("--watch", action="store_true", help="Refresh every 2s (Ctrl-C to stop).")
    args = parser.parse_args()

    tick = threading.Event()  # interruptible timer; never set
    watching = True
    while watching:
        names = [args.name] if args.name else _browser_names()
        if args.watch:
            print("\033[2J\033[H", end="")
        if not names:
            print("no browsers in the fleet -- open one from the + menu first")
        for name in names:
            status, payload = _get(f"/browsers/{name}/latency")
            if status != 200:
                print(f"browser {name}: {payload.get('error', f'latency failed ({status})')}", file=sys.stderr)
                if not args.watch:
                    return 1
                continue
            _render(name, payload)
        print("note: transport is TCP; packet loss is invisible to it and shows up here as rtt spikes.")
        watching = bool(args.watch)
        if watching:
            tick.wait(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
