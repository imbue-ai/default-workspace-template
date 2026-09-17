"""The flow lab: the UI-flow loop against a local app, with no box, no workspace and no proxy.

At trial time a flow's browser runs in the box and drives the delivered app through the forward
proxy, while the loop that decides and records each step runs host-side. The lab keeps everything
from the step script down exactly as the box runs it -- the script itself, exec'd as a one-shot
process per step the way the box execs it, the same Chromium flags, the same aria snapshot -- and
drops only the box transport and the proxy in front of the app. The app is a directory of static
files served on a local port: a fixture under `flow_lab_apps/`, or an app pulled out of a trial's
deliverable bundle to reproduce a flow that went wrong there without paying for the trial.

Two entry points use it. The tests in `test_flow_lab.py` drive the fixtures with a scripted agent,
so a change to the executor or the state summariser is measured against known page behaviours.
`minds-evals flow-lab` drives any local app with the real verification agent, which is how a prompt
rule is tried out.
"""

import json
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler
from http.server import ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals import flow_browser
from imbue.minds_evals import flow_runner
from imbue.minds_evals import minds_bridge
from imbue.minds_evals import ui_flows
from imbue.minds_evals.data_types import FlowSurface
from imbue.minds_evals.data_types import UiFlowCheck
from imbue.minds_evals.evidence_collection import FLOW_LOG_FILENAME
from imbue.minds_evals.expectations import slugify
from imbue.minds_evals.resources.flow_step_protocol import StepReaction
from imbue.mngr.utils.polling import poll_until


class _QuietRequestHandler(SimpleHTTPRequestHandler):
    """Serves files without logging every request to stderr, which would drown a test's output."""

    def log_message(self, format: str, *args: Any) -> None:
        return


@contextmanager
def serve_static_app(app_dir: Path) -> Iterator[str]:
    """Serve a directory as an app's origin on a port of its own, yielding the origin URL (with a
    trailing slash, so a page path or query appends to it directly)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: _QuietRequestHandler(*args, directory=str(app_dir)))
    thread = threading.Thread(target=server.serve_forever, name="flow-lab-app", daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}/".format(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class LocalFlowStepExecutor(flow_runner.FlowStepExecutor):
    """Runs the box's step script here, as a subprocess against a local browser.

    The script is the same file the box uploads and runs, invoked the same way -- one process per
    step, the request as its one argument, its reply as the JSON on its stdout -- under this
    project's own interpreter, which holds the same playwright the box's venv does. The frames land
    in a directory of the caller's choosing rather than the box's evidence directory.
    """

    cdp_endpoint_url: str = Field(frozen=True, description="Where the local browser listens for CDP")
    screenshot_dir: Path = Field(frozen=True, description="Where each step's frame is written")
    concurrency_group: ConcurrencyGroup = Field(frozen=True, description="Owns every step script process")
    step_timeout_seconds: float = Field(
        frozen=True, default=flow_runner.STEP_TIMEOUT_SECONDS, description="How long one step script may run"
    )

    async def run_step(self, action: ui_flows.FlowAction, step_index: int) -> ui_flows.StepOutcome:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        request = ui_flows.build_step_request(
            action,
            str(self.screenshot_dir / ui_flows.flow_screenshot_name(step_index)),
            cdp_endpoint_url=self.cdp_endpoint_url,
            preauth_cookie="",
            cookie_domain="",
        )
        # Blocks the loop for the step's duration. The interface is async because harbor's is; the
        # lab runs one flow at a time, so nothing else is waiting on the loop meanwhile.
        with resources.as_file(minds_bridge.flow_step_script()) as script_path:
            finished = self.concurrency_group.run_process_to_completion(
                [sys.executable, str(script_path), request],
                timeout=self.step_timeout_seconds,
                is_checked_after=False,
                name="box_flow_step",
            )
        if finished.is_timed_out:
            return ui_flows.StepOutcome(
                is_ok=False,
                reason=ui_flows.REASON_STEP_BRIDGE_FAILED,
                detail="the step script did not answer within {}s".format(self.step_timeout_seconds),
                state_text="",
                screenshot_name="",
                reaction=StepReaction.UNOBSERVED,
            )
        # A script that never got to print its verdict has said why on stderr, which is the only
        # thing worth reporting when stdout carries no reply.
        return ui_flows.parse_step_result(finished.stdout or finished.stderr)


@pure
def lab_flow_check(name: str, actions: str, expect: str) -> UiFlowCheck:
    """A flow declared on the spot, shaped as the generator would expand it from a case."""
    return UiFlowCheck(
        check_id="ui_flow_lab_{}".format(slugify(name)),
        name=name,
        actions=actions,
        expect=expect,
        surface=FlowSurface.ORIGIN,
    )


# How long the profile cleanup keeps retrying after the browser is gone. Chromium's
# helpers exit within a fraction of a second of the browser process on a healthy
# machine; the budget is for a loaded CI runner.
_PROFILE_CLEANUP_TIMEOUT_SECONDS: Final[float] = 10.0
_PROFILE_CLEANUP_POLL_SECONDS: Final[float] = 0.2


class _ProfileRemoval(MutableModel):
    """Takes a profile tree down, keeping what the latest attempt ran into so a tree that is left
    behind is reported with its reason rather than a guess.

    Only the first failure of an attempt is kept: rmtree carries on past it and then fails to remove
    each ancestor with "Directory not empty", which says nothing the caller does not already know.
    """

    directory: Path = Field(frozen=True, description="The profile tree to remove")
    cause: str = Field(default="", description="The first failure of the latest attempt, if it had any")

    def try_remove(self) -> bool:
        self.cause = ""
        shutil.rmtree(self.directory, onexc=self._record_first_error)
        return not self.directory.exists()

    def _record_first_error(self, function: Callable[..., Any], path: str, exc: BaseException) -> None:
        if not self.cause:
            self.cause = "{}: {}".format(path, exc)


@contextmanager
def fresh_profile_dir() -> Iterator[Path]:
    """A browser profile directory of its own, removed on exit once Chromium has let go of it.

    Terminating the browser waits for the browser process alone. Its renderer, GPU and
    crashpad helpers exit a moment later and can still be writing under ``Default/`` while the
    directory is removed, so one ``rmtree`` can find a directory refilled behind it and fail on
    the final ``rmdir``. The removal is retried until the tree stays gone; a tree that never
    does is left behind, with a warning naming what the last attempt ran into, rather than
    failing the run over a temp directory.
    """
    profile_dir = Path(tempfile.mkdtemp(prefix="minds-evals-flow-lab-"))
    try:
        yield profile_dir
    finally:
        removal = _ProfileRemoval(directory=profile_dir)
        if not poll_until(
            removal.try_remove,
            timeout=_PROFILE_CLEANUP_TIMEOUT_SECONDS,
            poll_interval=_PROFILE_CLEANUP_POLL_SECONDS,
        ):
            logger.warning(
                "Left the browser profile at {} behind after {}s of removals; the last one {}",
                profile_dir,
                _PROFILE_CLEANUP_TIMEOUT_SECONDS,
                "failed with {}".format(removal.cause)
                if removal.cause
                else "reported no error, yet the directory is still there",
            )


async def run_lab_flow(
    app_dir: Path,
    page: str,
    check: UiFlowCheck,
    agent: ui_flows.VerificationAgent,
    output_dir: Path,
    chromium_path: Path,
    flow_deadline_seconds: float = flow_runner.FLOW_DEADLINE_SECONDS,
) -> flow_runner.FlowRun:
    """Serve `app_dir`, open `page` on it in a fresh browser, and drive one flow to its end.

    Leaves the same evidence a trial's flow directory holds -- `log.jsonl` and the `step_NNN.png`
    frames -- in `output_dir`, so the viewer's readers and the judge's digest renderer can be
    pointed at it. `page` is appended to the served origin: empty for its index, or a query such as
    `?latency=300` to select a fixture's behaviour.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    with (
        ConcurrencyGroup(name="flow-lab") as concurrency_group,
        serve_static_app(app_dir) as origin,
        fresh_profile_dir() as profile_dir,
        flow_browser.launch_local_browser(chromium_path, profile_dir, concurrency_group) as cdp_endpoint_url,
    ):
        executor = LocalFlowStepExecutor(
            cdp_endpoint_url=cdp_endpoint_url, screenshot_dir=output_dir, concurrency_group=concurrency_group
        )
        run = await flow_runner.run_flow(
            check,
            origin + page,
            agent,
            executor,
            # No collection phase to run out of: the flow's own deadline is the only one.
            phase_deadline=float("inf"),
            flow_deadline=time.monotonic() + flow_deadline_seconds,
        )
    (output_dir / FLOW_LOG_FILENAME).write_text("".join(line + "\n" for line in run.records))
    return run


@pure
def describe_record(record_json: str) -> str:
    """One flow log line as a reader would say it, for the lab's console output."""
    record = json.loads(record_json)
    kind = record.get("kind")
    if kind == ui_flows.FlowRecordKind.INIT.value:
        return "opened {}".format(record.get("url"))
    if kind == ui_flows.FlowRecordKind.FINAL.value:
        return "reading: {}".format(record.get("observation") or "(none recorded)")
    return "{}. {}".format(
        record.get("step_index"),
        ui_flows.describe_step(
            str(record.get("action") or ""),
            str(record.get("expected") or ""),
            str(record.get("error") or ""),
            str(record.get("observed") or ""),
        ),
    )
