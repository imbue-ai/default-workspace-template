"""Live-view self-check: measure how long opening a browser really takes, and where.

Run it INSIDE a Minds workspace (from the repo root) to see, on the real hardware:

    uv run python libs/browser/scripts/selfcheck.py

It launches one throwaway browser exactly the way the fleet does, times each phase
(X display -> Chromium launch -> stream first frame), reports whether the video
encoder (pixelflux) is actually available, and tears it down. No service needed.

Reading the output:
  * "video encoder: MISSING" -> the live view will show "Loading page…" forever
    because there's nothing to stream. deferred-install may still be running; check
    `supervisorctl status deferred-install`.
  * "Chromium launch" is the dominant cost -- a headful browser cold-starting on a
    GPU-less box. That's the wait you see behind "Starting browser…".
  * "first video frame" should be well under a second once the encoder is present.
"""

import asyncio
import os
import queue
import sys
import time

os.environ.setdefault("BROWSER_HEADLESS", "0")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

from loguru import logger  # noqa: E402

from browser import capture, session  # noqa: E402
from browser.display import Display  # noqa: E402

# Clean, prefix-free stdout: this is a user-facing report, not service logging.
logger.remove()
logger.add(sys.stdout, format="{message}", level="INFO")


async def main() -> None:
    logger.info("=== live-view self-check ===")
    present = "present" if os.path.exists(session._FORTRESS_EXECUTABLE) else "MISSING"
    logger.info(f"Fortress executable : {session._FORTRESS_EXECUTABLE}  ({present})")
    have_encoder = capture._load_pixelflux() is not None
    logger.info(
        "video encoder       : "
        + ("available" if have_encoder else "MISSING (no live view until its native libs install)")
    )

    phases: dict[str, float] = {}
    orig_bu = session.LiveBrowser._start_bu_session
    orig_disp = Display.start

    async def timed_bu(self: session.LiveBrowser, *a: object, **k: object) -> object:
        started = time.monotonic()
        result = await orig_bu(self, *a, **k)  # type: ignore[arg-type]
        phases["Chromium launch"] = time.monotonic() - started
        return result

    async def timed_disp(self: Display) -> None:
        started = time.monotonic()
        await orig_disp(self)
        phases["X display"] = time.monotonic() - started

    session.LiveBrowser._start_bu_session = timed_bu  # type: ignore[assignment]
    Display.start = timed_disp  # type: ignore[assignment]

    manager = session.BrowserSessionManager()
    launched_at = time.monotonic()
    try:
        browser = await manager.create()
        for task in list(manager._launch_tasks):
            await task
        phases["create -> running (total)"] = time.monotonic() - launched_at

        subscribed_at = time.monotonic()
        client_queue = await browser.add_stream_subscriber(True)
        if client_queue is None:
            logger.info("\n!!! stream did not start: the browser is running but there is NO encoder.")
            logger.info("    The live view would sit on 'Loading page…' forever here.")
        else:
            first_frame = None
            for _ in range(200):  # up to ~10s
                await asyncio.sleep(0.05)
                try:
                    first_frame = client_queue.get_nowait()
                    break
                except queue.Empty:
                    pass
            if first_frame is None:
                logger.info("\n!!! encoder started but produced NO frame within 10s -- capture is broken.")
            else:
                phases["first video frame"] = time.monotonic() - subscribed_at
            await browser.remove_stream_subscriber(client_queue)

        logger.info("\nphase timings:")
        for label in ("X display", "Chromium launch", "create -> running (total)", "first video frame"):
            if label in phases:
                logger.info(f"  {phases[label]:6.2f}s  {label}")
    finally:
        await manager.shutdown()
    logger.info("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
