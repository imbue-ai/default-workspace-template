"""The Chromium a UI flow drives: the flags every launch uses, and a local launch for the flow lab.

Kept free of harbor on purpose: `conftest.py` imports this module to launch a browser for the lab's
tests, and the ROOT pytest run loads that conftest from a venv that has no harbor, which nearly
every other module of this package imports.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from playwright.sync_api import sync_playwright
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals.errors import FlowBrowserError
from imbue.mngr.utils.polling import wait_for

# How the flow browser is launched, wherever it runs; the port and the profile are added per launch.
# --no-sandbox because the box runs as root, which is where Chromium refuses to start its sandbox;
# --disable-dev-shm-usage because a container's default /dev/shm is too small for Chromium's
# renderer and it crashes in ways that read as a broken app; --ignore-certificate-errors because the
# forward proxy serves the app on a self-signed origin; --use-mock-keychain because on macOS Chrome
# otherwise blocks on the login keychain before it ever serves CDP; --disable-field-trial-config
# because the build's baked-in experiment config otherwise turns on whatever features it lists,
# and with it on page.screenshot() over CDP never gets past "waiting for fonts to load" on macOS.
# Both are flags playwright itself passes on every platform, and no-ops where they do not
# apply. The flow lab launches with the same flags, so the page it captures is rendered the way the
# box renders it.
# --window-size is the window the product's own browser service opens a page in, so a flow renders
# the app at the size a user's pane renders it at. A step captures the viewport rather than the full
# page, which makes this the flag that decides how much of the app reaches the judge.
CHROMIUM_LAUNCH_FLAGS: Final[tuple[str, ...]] = (
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--remote-debugging-address=127.0.0.1",
    "--ignore-certificate-errors",
    "--use-mock-keychain",
    "--disable-field-trial-config",
    "--window-size=1280,800",
)

# What Chromium prints on stderr once its debug server is up, naming the port it bound. Asking for
# port 0 and reading the port off this line is what makes a local launch free of port races.
_DEVTOOLS_LISTENING: Final[re.Pattern[str]] = re.compile(r"DevTools listening on ws://127\.0\.0\.1:(\d+)/")
_READY_TIMEOUT_SECONDS: Final[float] = 30.0
# The profile is thrown away, so a browser that is slow to honour SIGTERM loses nothing by being
# killed; a short grace keeps teardown quick on a loaded machine, where a graceful exit can take far
# longer than the browser's whole useful life.
_SIGTERM_GRACE_SECONDS: Final[float] = 3.0
# How long stopping the browser may take before it counts as broken. It has to outlast the grace, the
# kill that follows it and the draining of the browser's output, all of which run before the process
# is reported finished; anything shorter reports a shutdown that is still on schedule as a failure.
_STOP_TIMEOUT_SECONDS: Final[float] = 30.0


def resolve_chromium_path() -> Path:
    """The full Chrome build playwright installed, resolved the way the box resolves its own.

    The path is computed whether or not the browser is installed; callers check it exists and say so
    with `missing_chromium_message`. Uses playwright's sync API, so it cannot be called from inside a
    running asyncio loop.
    """
    with sync_playwright() as playwright:
        return Path(playwright.chromium.executable_path)


@pure
def missing_chromium_message(chromium_path: Path) -> str:
    """What to tell whoever asked for a browser that is not installed, including how to install it.

    Lives beside the resolution rather than at each caller: the install command is a fact about
    which browser `resolve_chromium_path` looks for, and every caller has to name the same one.
    """
    return (
        "playwright's chromium is not installed at {}; run `uv run python -m playwright install chromium` "
        "in apps/minds_evals".format(chromium_path)
    )


class _DevToolsListener(MutableModel):
    """Watches the browser's output for the line that names its debug port."""

    port: int | None = Field(default=None, description="The port Chromium bound, once it said so")

    def on_output(self, line: str, is_stdout: bool) -> None:
        match = _DEVTOOLS_LISTENING.search(line)
        if match is not None:
            self.port = int(match.group(1))


@contextmanager
def launch_local_browser(chromium_path: Path, profile_dir: Path, concurrency_group: ConcurrencyGroup) -> Iterator[str]:
    """A headless Chromium on a fresh profile, yielding the CDP endpoint the step script connects to.

    Launched the way the box launches its own -- the same flags, a profile of its own so nothing
    persists between launches -- and terminated on exit, whatever the body did. The process is the
    group's, so a lab run that dies mid-flow still takes its browser down with it.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    listener = _DevToolsListener()
    process = concurrency_group.run_process_in_background(
        [
            str(chromium_path),
            *CHROMIUM_LAUNCH_FLAGS,
            "--remote-debugging-port=0",
            "--user-data-dir={}".format(profile_dir),
        ],
        on_output=listener.on_output,
        # Terminated by this context, so its exit code is never a failure; and its output is a
        # permanent stream that must not be retained for the browser's whole life.
        is_checked_by_group=False,
        is_output_accumulated=False,
        name="flow-lab-chromium",
        shutdown_timeout_sec=_SIGTERM_GRACE_SECONDS,
    )
    try:
        ready_message = "chromium did not serve CDP within {}s".format(_READY_TIMEOUT_SECONDS)
        try:
            wait_for(
                lambda: listener.port is not None or process.is_finished(),
                timeout=_READY_TIMEOUT_SECONDS,
                error_message=ready_message,
            )
        except TimeoutError as exc:
            # wait_for signals a condition that never held with the builtin. Both ways a launch can
            # fail belong to this module's own error, so a caller has one thing to catch.
            raise FlowBrowserError(ready_message) from exc
        if listener.port is None:
            raise FlowBrowserError("chromium exited with status {} before serving CDP".format(process.returncode))
        yield "http://127.0.0.1:{}".format(listener.port)
    finally:
        process.terminate(force_kill_seconds=_STOP_TIMEOUT_SECONDS)
