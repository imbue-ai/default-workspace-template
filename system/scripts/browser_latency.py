#!/usr/bin/env python3
"""Live-view latency readout, in Citrix's ICA decomposition.

    python3 system/scripts/browser_latency.py [name] [--watch]

The industry-standard shape for this measurement (Citrix HDX) splits the
user-visible total into its causes:

    ICA RTT  =  ICA Latency  +  Host Delay  +  Endpoint Delay
    (total)     (network)       (server)      (client)

We measure the first two directly and report the remainder as one combined
"processing" figure: splitting server from client needs cooperation from the
VNC server that ours does not provide, so an honest single number beats a
fabricated split.

  ICA RTT       input -> pixels on screen. What the user feels, from REAL
                clicks -- the only series that travels the true input path.
                Graded against the published Citrix thresholds (great <180ms,
                good <240ms).
  ICA Latency   a periodic timestamp echo over the cast socket -- the same
                network path the video rides. The floor; nothing beats it.
  processing    ICA RTT minus ICA Latency: render + encode + transport-of-bytes
                + decode + paint.

  srv>glass     a fixed-size repaint the daemon triggers, timed from the moment
                the server reports the change committed until the pixels land.
                Excludes input injection (the daemon triggers it over CDP, a
                path no user input takes), so it isolates encode + bytes +
                decode + paint -- the half encoding and transport work moves --
                and is comparable across runs because the repaint never varies.
                Uncontrolled click sampling is not comparable: a link repaints a
                viewport, a checkbox a few hundred pixels.

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


def _grade(ica_rtt_ms: float) -> str:
    """Citrix's published ICA RTT bands for interactive remote sessions."""
    if ica_rtt_ms < 180:
        return "GREAT"
    if ica_rtt_ms < 240:
        return "GOOD"
    return "POOR"


def _series(label: str, stats: dict[str, Any] | None, hint: str) -> None:
    if stats is None:
        print(f"  {label:<13} -- no samples yet; {hint}")
        return
    line = f"  {label:<13} p50 {stats['p50_ms']:7.1f}ms   p95 {stats['p95_ms']:7.1f}ms   (n={stats['n']})"
    if "spikes_recent" in stats:
        line += f"   spikes {stats['spikes_recent']}/{stats['spike_window']}"
    print(line)


def _render(name: str, payload: dict[str, Any]) -> None:
    ica = payload.get("ica") or {}
    total = ica.get("ica_rtt_ms")
    network = ica.get("ica_latency_ms")
    processing = ica.get("processing_ms")

    print(f"browser {name}")
    if total is None or network is None:
        print("  (waiting for samples -- open this browser's pane and leave it open a few seconds)")
    else:
        share = f"{100 * network / total:.0f}%" if total else "?"
        print(f"  ICA RTT       {total:7.1f}ms   [{_grade(total)}]   great <180, good <240")
        print(f"    ICA Latency {network:7.1f}ms   network -- the floor ({share} of total)")
        print(f"    processing  {processing:7.1f}ms   render + encode + bytes + decode + paint")
        glass = ica.get("server_to_glass_ms")
        if glass is not None:
            print(f"      of which  {glass:7.1f}ms   server->glass (encode + bytes + decode + paint)")

    print("  --")
    _series("srv>glass", payload.get("server_to_glass"), "the pane probes every 4s once open")
    _series("click>photon", payload.get("click_photon"), "click inside the live view")
    _series("rtt", payload.get("rtt"), "open this browser's pane")


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
        print("note: transport is TCP; packet loss is invisible to it and shows up as rtt spikes.")
        print("      srv>glass = fixed-size repaint, comparable across runs; clicks are ground truth.")
        watching = bool(args.watch)
        if watching:
            tick.wait(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
