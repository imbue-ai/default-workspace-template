"""Live-view self-check: one command that says WHY opening a browser is slow or stuck.

Run it INSIDE a Minds workspace, from the repo root:

    uv run python libs/browser/scripts/selfcheck.py

You do NOT need to open a browser first -- section 4 launches its own throwaway one and
times it. But if you already have a browser open that's misbehaving, leave it open:
section 3 connects to the LIVE service and tests that exact browser's video stream, which
is the real "Loading page… forever" path.

What it reports:
  1. Environment & deps  -- Fortress/Xvfb/xclip present, env-converge units done, and
     whether the video encoder (pixelflux) actually imports (with the exact missing lib).
  2. Running service     -- is browser-service up, what browsers exist and their state,
     and any recent errors in its log.
  3. Live stream test    -- subscribe to a real running browser's video and time the
     first frame (this is what "doesn't load" is really failing at).
  4. Launch benchmark    -- cold-launch a throwaway browser and time each phase.

Reading it: "video encoder: MISSING" or a MISSING native lib => the live view can never
paint (it will sit on "Loading page…") until env-converge finishes. Otherwise the
wait is the "Chromium launch" line -- a real headful browser cold-starting.
"""

import asyncio
import importlib.metadata as md
import logging
import os
import queue
import shutil
import subprocess
import sys
import time

os.environ.setdefault("BROWSER_HEADLESS", "0")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("BROWSER_USE_LOGGING_LEVEL", "error")  # keep the report readable

import requests  # noqa: E402
import websocket  # noqa: E402  (websocket-client)
from loguru import logger  # noqa: E402

from browser import capture, session  # noqa: E402
from browser.display import Display  # noqa: E402

# Clean, prefix-free stdout: this is a user-facing report, not service logging. loguru is
# separate from stdlib logging, so silencing INFO-and-below (browser-use's chatter) below
# leaves this script's own loguru report lines intact.
logger.remove()
logger.add(sys.stdout, format="{message}", level="INFO")
logging.disable(logging.INFO)

SERVICE_URL = os.environ.get("BROWSER_SERVICE_URL", "http://localhost:8081")
SERVICE_LOG = "/var/log/supervisor/browser-stderr.log"
# The native libs pixelflux's wheel links at import time (installed by the env.d
# unit system/scripts/env.d/1010-browser-display-audio.sh).
REQUIRED_LIBS = ["libva.so.2", "libva-drm.so.2", "libva-x11.so.2", "libgbm.so.1", "libdrm.so.2"]


def _row(label: str, value: str) -> None:
    logger.info(f"  {label:<22}: {value}")


def check_env() -> None:
    _row("running as root", "yes" if os.geteuid() == 0 else "no")
    fortress = session._FORTRESS_EXECUTABLE
    _row("Fortress executable", f"{fortress}  ({'present' if os.path.exists(fortress) else 'MISSING'})")
    for tool in ("Xvfb", "xclip"):
        _row(f"{tool} binary", shutil.which(tool) or "MISSING")
    try:
        stat = os.statvfs("/dev/shm")
        _row("/dev/shm size", f"{stat.f_blocks * stat.f_frsize / 1e9:.1f} GB")
    except OSError:
        pass

    ready, reason = session.deferred_install_ready()
    _row("env-converge install", "ready" if ready else f"NOT ready -- {reason}")

    encoder = capture._load_pixelflux()
    _row("video encoder", "available" if encoder else "MISSING (no live view until the libs below land)")
    try:
        ldcache = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        ldcache = ""
    for lib in REQUIRED_LIBS:
        _row(f"  {lib}", "found" if lib in ldcache else "MISSING")

    for pkg in ("browser-use", "playwright", "pixelflux", "python-xlib"):
        try:
            _row(f"version {pkg}", md.version(pkg))
        except md.PackageNotFoundError:
            _row(f"version {pkg}", "not installed")
    if os.path.exists(fortress):
        try:
            out = subprocess.run([fortress, "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
            _row("chromium --version", out or "(no output)")
        except (OSError, subprocess.SubprocessError) as e:
            _row("chromium --version", f"failed: {e}")


def _fleet() -> dict:
    return requests.get(f"{SERVICE_URL}/browsers", timeout=5).json()


def check_service() -> None:
    try:
        data = _fleet()
    except requests.RequestException:
        _row("service reachable", "NO -- could not connect")
        logger.info("  browser-service is not answering. Check `supervisorctl status browser`.")
        return
    _row("service reachable", f"yes ({SERVICE_URL})")
    _row("can create browser", f"{data.get('can_create')} ({data.get('create_reason') or 'ok'})")
    _row("browsers open", f"{data.get('browser_count')}/{data.get('browser_max')}")
    for browser in data.get("browsers", []):
        logger.info(
            f"    - {browser['id']}: lifecycle={browser['lifecycle']} crashed={browser['crashed']} "
            f"tabs={len(browser.get('tabs', []))} owner={browser.get('owner_name')}"
        )
    _tail_service_log()


def _tail_service_log() -> None:
    if not os.path.exists(SERVICE_LOG):
        logger.info(f"  (no service log at {SERVICE_LOG})")
        return
    try:
        with open(SERVICE_LOG, errors="replace") as handle:
            lines = handle.readlines()[-500:]
    except OSError:
        return
    needles = ("error", "exception", "traceback", "pixelflux", "missing", "failed", "importerror")
    bad = [ln.rstrip() for ln in lines if any(n in ln.lower() for n in needles)]
    if bad:
        logger.info("  recent errors/warnings in the service log:")
        for ln in bad[-12:]:
            logger.info(f"    {ln[:200]}")
    else:
        logger.info("  service log: no recent errors")


def stream_live_browser() -> None:
    try:
        data = _fleet()
    except requests.RequestException:
        logger.info("  (service not reachable; skipping)")
        return
    running = [b for b in data.get("browsers", []) if b.get("lifecycle") == "running" and not b.get("crashed")]
    if not running:
        logger.info("  no running browser to test -- open one in the workspace and re-run to exercise the LIVE path.")
        return
    browser_id = running[0]["id"]
    url = SERVICE_URL.replace("http", "ws", 1) + f"/browsers/{browser_id}/stream"
    logger.info(f"  connecting to {url}")
    started = time.monotonic()
    try:
        sock = websocket.create_connection(url, timeout=15)
        sock.send('{"h264": false}')  # JPEG mode: confirming bytes flow needs no decoder
        sock.settimeout(15)
        frame = sock.recv()
        sock.close()
    except (websocket.WebSocketException, OSError) as e:
        logger.info(f"  STREAM FAILED against '{browser_id}': {type(e).__name__}: {e}")
        logger.info("  -> this IS the 'Loading page… forever' failure. Fix the encoder/libs in section 1.")
        return
    if isinstance(frame, (bytes, bytearray)) and frame:
        _row(f"first frame from '{browser_id}'", f"{time.monotonic() - started:.2f}s ({len(frame)} bytes)  OK")
    else:
        logger.info(f"  '{browser_id}' sent a non-frame message; the encoder may be down.")


async def bench_launch() -> None:
    phases: dict[str, float] = {}
    orig_bu = session.LiveBrowser._start_bu_session
    orig_disp = Display.start

    async def timed_bu(self: session.LiveBrowser, *a: object, **k: object) -> object:
        at = time.monotonic()
        result = await orig_bu(self, *a, **k)  # type: ignore[arg-type]
        phases["Chromium launch"] = time.monotonic() - at
        return result

    async def timed_disp(self: Display) -> None:
        at = time.monotonic()
        await orig_disp(self)
        phases["X display"] = time.monotonic() - at

    session.LiveBrowser._start_bu_session = timed_bu  # type: ignore[assignment]
    Display.start = timed_disp  # type: ignore[assignment]

    manager = session.BrowserSessionManager()
    started = time.monotonic()
    browser = await manager.create()
    try:
        for _ in range(1200):  # up to ~60s to reach "running" (when the viewer's overlay clears)
            if browser._is_running:
                break
            await asyncio.sleep(0.05)
        phases["create -> running"] = time.monotonic() - started

        subscribed_at = time.monotonic()
        client_queue = await browser.add_stream_subscriber(True)
        if client_queue is None:
            logger.info("  encoder did NOT start -> the live view would hang on 'Loading page…'. See section 1.")
        else:
            first_frame = None
            for _ in range(200):  # ~10s
                await asyncio.sleep(0.05)
                try:
                    first_frame = client_queue.get_nowait()
                    break
                except queue.Empty:
                    pass
            if first_frame is None:
                logger.info("  encoder started but produced NO frame in 10s -> capture is broken.")
            else:
                phases["first video frame"] = time.monotonic() - subscribed_at
            await browser.remove_stream_subscriber(client_queue)

        for label in ("X display", "Chromium launch", "create -> running", "first video frame"):
            if label in phases:
                _row(label, f"{phases[label]:6.2f}s")
    finally:
        await manager.shutdown()


async def _section(name: str, run: object) -> None:
    logger.info(f"\n[{name}]")
    try:
        result = run() if callable(run) else run
        if asyncio.iscoroutine(result):
            await result
    except Exception as e:  # a diagnostic must report the checks that DO work even if one probe throws
        logger.info(f"  (this check errored: {type(e).__name__}: {e})")


async def main() -> None:
    logger.info("=== live-view self-check ===")
    await _section("1. Environment & dependencies", check_env)
    await _section("2. Running browser service", check_service)
    await _section("3. Live stream test (existing browser)", stream_live_browser)
    await _section("4. Cold-launch benchmark (throwaway browser)", bench_launch)
    logger.info("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
