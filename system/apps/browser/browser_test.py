import asyncio
import json
import queue
import shutil
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any

import pytest
from app_instances.testing import RecordingNudger
from browser import chrome_args
from browser import chrome_launcher
from browser import manifest
from browser import runner
from browser import session as bsession
from browser.bridged_fleet import BridgedFleet
from browser.data_types import BrowserController
from browser.data_types import BrowserLifecycle
from browser.data_types import BrowserSnapshot
from browser.errors import FleetCreateRefusedError
from browser.errors import FleetUnavailableError
from browser.errors import NavigationFailedError
from browser.primitives import BrowserName
from mock_cdp_client_test import NavigatingCdpClient


async def _noop_wake(self: bsession.LiveBrowser, agent_id: str, agent_name: str | None) -> None:
    """Stand-in for ``_wake_agent`` in tests: skip the real ``mngr message`` subprocess."""


def _running_browser(browser_id: str) -> bsession.LiveBrowser:
    """A LiveBrowser already in the ``running`` lifecycle, for the ownership / state-machine
    / cast tests that exercise behaviour available only once Chromium is up. A freshly
    constructed LiveBrowser is ``init`` (Chromium not launched yet), where acquire/run
    return ``starting``; these tests assume a live browser, so they start it ``running``."""
    browser = bsession.LiveBrowser(browser_id=browser_id)
    browser._lifecycle = "running"
    return browser


def _pop_json(cast_queue: "queue.Queue[str | None]") -> dict[str, Any]:
    """Pop the next cast-queue payload and parse it as JSON.

    A cast queue holds JSON strings, with ``None`` reserved as the shutdown sentinel
    (never enqueued in these tests). Asserting it isn't ``None`` narrows the type for
    ``json.loads`` and explodes loudly if a sentinel ever leaked in."""
    payload = cast_queue.get_nowait()
    assert payload is not None, "unexpected shutdown sentinel on the cast queue"
    return json.loads(payload)


# --- env / key helpers (unchanged) -------------------------------------------


def test_deferred_install_ready_gates_on_fortress_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROWSER_SKIP_INSTALL_CHECK", raising=False)
    # Isolate the fortress-executable gate from the headful Xvfb gate (which the pixelflux
    # media path adds): force headless so readiness turns only on the Chromium binary.
    monkeypatch.setattr(bsession, "_HEADLESS", True)
    # The check is os.access(_, X_OK), so the fake binary must live on an EXECUTABLE
    # filesystem. pytest's tmp_path can be a noexec tmpfs (chmod +x still yields X_OK
    # False there), so stage it under this app dir (a normal ext4 checkout) instead.
    staging = Path(tempfile.mkdtemp(dir=Path(__file__).parent))
    fortress = staging / "tilion"
    monkeypatch.setattr(bsession, "_FORTRESS_EXECUTABLE", str(fortress))
    try:
        # Missing binary: still installing.
        ready, _ = bsession.deferred_install_ready()
        assert ready is False
        # Present but not executable (a partially-staged install): still not ready.
        fortress.write_text("")
        ready, _ = bsession.deferred_install_ready()
        assert ready is False
        fortress.chmod(0o755)
        ready, reason = bsession.deferred_install_ready()
        assert ready is True
        assert reason == "ready"
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# --- ownership state machine (no browser needed) -----------------------------


def test_acquire_release_is_compare_and_set() -> None:
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        assert await browser.acquire("A", "Alice") == "acquired"
        assert browser._state_tuple() == ("agent", "A", False)
        # A second agent can't grab it; with --no-wait it fails fast.
        assert await browser.acquire("B", "Bob", wait=False) == "busy_agent"
        # The same agent re-acquiring is idempotent.
        assert await browser.acquire("A") == "acquired"
        # Only the owner can release; a double / non-owner release is a safe no-op.
        assert await browser.release("A") is True
        assert browser._state_tuple() == ("human", None, False)
        assert await browser.release("A") is False
        assert await browser.release("B") is False

    asyncio.run(go())


def test_input_gating_follows_controller() -> None:
    # Human input now flows over the pixelflux /stream socket as XTEST, gated on the
    # thread-safe _input_gate mirror of _input_enabled (mediastream reads it off-loop).
    # This checks the gate tracks the controller; the actual XTEST injection is covered
    # by the mediastream/xinput path and live verification, not this unit.
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        # Agent in control: human input is gated off (the input/control TOCTOU guard),
        # on both the asyncio Event and its thread-safe /stream mirror.
        await browser.acquire("A")
        assert not browser._input_enabled.is_set()
        assert not browser._input_gate.is_set()
        assert browser.input_allowed is False
        # Released back to the human: both flip back on and input flows again.
        await browser.release("A")
        assert browser._input_enabled.is_set()
        assert browser._input_gate.is_set()
        assert browser.input_allowed is True

    asyncio.run(go())


def test_take_control_preempts_pins_and_reclaim_resumes() -> None:
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        # Human take-control always wins: pinned human, input re-enabled.
        assert await browser.take_control() is True
        assert browser._state_tuple() == ("human", None, True)
        assert browser._input_enabled.is_set()
        # While pinned, agents are locked out -- even with wait they get busy_human.
        assert await browser.acquire("B", "Bob", wait=False) == "busy_human"
        assert await browser.acquire("B", "Bob", wait=True, max_wait=0.1) == "busy_human"
        # Only an explicit reclaim (the human told the agent to resume) takes it back.
        assert await browser.acquire("B", "Bob", reclaim=True) == "acquired"
        assert browser._state_tuple() == ("agent", "B", False)

    asyncio.run(go())


def test_take_control_is_gated_on_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    # A human take-control on a browser whose Chromium isn't up (init) or is gone
    # (crashed) must NOT pin it (finding [2]): pinning an init browser before it's
    # running would bring it up locked to the human and lock every agent out. It no-ops
    # (returns False, no transition) until the browser is running; once running, the
    # take lands normally.
    casts: list[dict[str, Any]] = []
    monkeypatch.setattr(bsession.LiveBrowser, "_broadcast", lambda self, message: casts.append(message))
    browser = bsession.LiveBrowser(browser_id="b1")  # init by default

    async def go() -> None:
        assert browser._lifecycle == "init"
        assert await browser.take_control() is False  # init -> ignored
        assert browser._state_tuple() == ("human", None, False)  # NOT pinned
        assert not any(m.get("human_pinned") for m in casts), "init take_control must not broadcast a pin"
        # A crashed browser is gone -- also a no-op.
        browser._lifecycle = "crashed"
        assert await browser.take_control() is False
        # Once running, take_control works as before (pins).
        browser._lifecycle = "running"
        assert await browser.take_control() is True
        assert browser._state_tuple() == ("human", None, True)

    asyncio.run(go())


def test_enqueue_on_busy_queues_for_resume_and_wakes_on_handback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Direct-control handoff: a human takes control, the agent's next command is
    # rejected (busy_human) and the agent is queued to resume. When the human hands
    # back, the queued agent is granted control and messaged to resume.
    woken: list[str | None] = []

    async def fake_wake(self: bsession.LiveBrowser, agent_id: str, agent_name: str | None) -> None:
        woken.append(agent_name)

    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", fake_wake)
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        await browser.take_control()  # human grabs it, pins (active)
        # A's next direct command is rejected AND it's queued to resume.
        assert await browser.acquire("A", "Alice", wait=False, enqueue_on_busy=True) == "busy_human"
        assert browser._waiting_names() == ["Alice"]
        # Human hands back -> A is granted control and woken to resume.
        assert await browser.return_to_agents() is True
        assert browser._state_tuple() == ("agent", "A", False)
        assert browser._waiting_names() == []  # dequeued on grant
        await asyncio.sleep(0)  # let the wake task run
        assert woken == ["Alice"]

    asyncio.run(go())


def test_agent_in_both_queues_is_not_re_granted_after_it_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    # An agent can land in BOTH queues: a rejected direct command queues it for resume,
    # then it runs an explicit blocking acquire and parks in the wait queue. When the
    # wait-queue grant fires it must be removed from the resume queue too, or releasing
    # later would spuriously re-grant the freed browser to the (now-done) agent.
    woken: list[str | None] = []

    async def fake_wake(self: bsession.LiveBrowser, agent_id: str, agent_name: str | None) -> None:
        woken.append(agent_name)

    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", fake_wake)
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")  # A holds it
        # B's direct command is rejected by A -> B queued for resume.
        assert await browser.acquire("B", "Bob", wait=False, enqueue_on_busy=True) == "busy_agent"
        assert browser._waiting_names() == ["Bob"]
        # B then parks in the connection-bound wait queue for the same browser.
        b_wait = asyncio.create_task(browser.acquire("B", "Bob", wait=True, max_wait=2.0))
        await asyncio.sleep(0)
        # A releases -> B is granted from the wait queue AND cleared from resume queue.
        await browser.release("A")
        assert await b_wait == "acquired"
        assert browser._state_tuple() == ("agent", "B", False)
        assert browser._waiting_names() == []  # not lingering in the resume queue
        # B finishes. Releasing must NOT re-grant to B (it would, if B were still queued).
        await browser.release("B")
        assert browser._state_tuple() == ("human", None, False)
        assert woken == []  # no spurious "handed back to you" wake

    asyncio.run(go())


def test_human_pin_is_sticky_with_no_idle_yield(monkeypatch: pytest.MonkeyPatch) -> None:
    # A human take-control is STICKY: it holds until the human explicitly hands back,
    # with no grace/idle yield -- even with an agent queued to resume. (A human can walk
    # away mid-CAPTCHA and the browser is never moved out from under them.)
    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", _noop_wake)
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        await browser.take_control()  # human pins
        assert await browser.acquire("A", "Alice", wait=False, enqueue_on_busy=True) == "busy_human"
        assert browser._waiting_names() == ["Alice"]
        # The only keepalive sweeps left act on agent ownership, never a human pin.
        assert await browser._sweep_unclaimed_grant() is False
        assert await browser._sweep_idle_lease() is False
        assert browser._state_tuple() == ("human", None, True)  # still pinned to the human
        assert await browser.acquire("A", "Alice", wait=False) == "busy_human"  # still locked out
        # Only an explicit hand-back returns it -- and the queued agent then resumes.
        assert await browser.return_to_agents() is True
        assert browser._state_tuple() == ("agent", "A", False)

    asyncio.run(go())


def test_resting_human_is_free_for_the_next_agent() -> None:
    # A *resting* human (controller=human, not pinned -- a fresh browser, or one an
    # agent's idle-lease released) is free: the next agent's command just takes it.
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        assert browser._state_tuple() == ("human", None, False)  # fresh = resting/free
        assert await browser.acquire("A", "Alice", wait=False) == "acquired"
        await browser.release("A")  # back to resting (not pinned)
        assert browser._state_tuple() == ("human", None, False)
        assert await browser.acquire("B", "Bob", wait=False) == "acquired"  # taken freely

    asyncio.run(go())


def test_handoff_to_human_fronts_resume_queue_and_announces(monkeypatch: pytest.MonkeyPatch) -> None:
    # An agent that hits a CAPTCHA hands the browser to the HUMAN (pinned, NOT the next
    # queued agent) and jumps to the FRONT of the resume queue, so it resumes first when
    # the human hands back. A distinct handoff_request is broadcast for the viewer.
    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", _noop_wake)
    casts: list[dict] = []

    def fake_broadcast(self: bsession.LiveBrowser, message: dict) -> None:
        casts.append(message)

    monkeypatch.setattr(bsession.LiveBrowser, "_broadcast", fake_broadcast)
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        # B is already queued behind A (its direct command was rejected).
        assert await browser.acquire("B", "Bob", wait=False, enqueue_on_busy=True) == "busy_agent"
        assert browser._waiting_names() == ["Bob"]
        # A hands off -> human pinned, A jumps to the FRONT of the queue (ahead of B).
        assert await browser.handoff("A", "Alice", "solve the CAPTCHA") is True
        assert browser._state_tuple() == ("human", None, True)
        assert browser._waiting_names() == ["Alice", "Bob"]
        announced = [m for m in casts if m.get("type") == "handoff_request"]
        assert announced and announced[-1]["reason"] == "solve the CAPTCHA"
        assert announced[-1]["agent_name"] == "Alice"
        # Hand-back goes to the requester (A), not B.
        assert await browser.return_to_agents() is True
        assert browser._state_tuple() == ("agent", "A", False)

    asyncio.run(go())


def test_handoff_is_a_noop_when_the_caller_does_not_hold_it() -> None:
    # Only the current owner can hand off; a stale/wrong caller changes nothing.
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        assert await browser.handoff("B", "Bob", "x") is False  # B never owned it
        assert browser._state_tuple() == ("agent", "A", False)  # unchanged
        await browser.take_control()  # a human now holds it
        assert await browser.handoff("A", "Alice", "x") is False  # A no longer owns it
        assert browser._state_tuple() == ("human", None, True)

    asyncio.run(go())


def test_should_disable_sandbox_when_running_as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # Chromium can't sandbox as root, so we disable it when euid==0 (the minds-workspace
    # case) and keep it for a non-root runtime (local dev, where the sandbox works).
    monkeypatch.setattr(bsession.os, "geteuid", lambda: 0)
    assert bsession._should_disable_sandbox() is True
    monkeypatch.setattr(bsession.os, "geteuid", lambda: 501)
    assert bsession._should_disable_sandbox() is False


def test_launch_args_keep_stealth_and_suppress_the_bad_flag_infobar() -> None:
    # As root we must pass --no-sandbox, which is on Chromium's kBadFlags list, so without
    # --test-type Chromium pins an "unsupported command-line flag" infobar over every page
    # -- and we film that window. --enable-automation would suppress it too, but it sets
    # navigator.webdriver, which defeats Fortress's whole point. Playwright's own default
    # switch list adds BOTH --enable-automation and --disable-extensions, which is exactly
    # why the launch does not go through Playwright: see chrome_args.
    args = chrome_args.launch_args(
        user_data_dir="/tmp/args-check", window_size=(1280, 800), extensions=("/opt/ext/ublock",), no_sandbox=True
    )
    assert "--test-type" in args
    assert "--enable-automation" not in args
    assert "--disable-extensions" not in args
    assert "--disable-blink-features=AutomationControlled" in args
    assert "--load-extension=/opt/ext/ublock" in args
    # The 1:1 window->capture mapping the streaming path depends on.
    assert "--window-position=0,0" in args


def test_launch_args_never_emit_a_playwright_anti_stealth_switch() -> None:
    # The assert inside launch_args is the real guard; this pins the intent so a future
    # edit that reintroduces one fails loudly rather than silently un-stealthing Fortress.
    for switch in chrome_args._STRIPPED_FROM_PLAYWRIGHT:
        assert switch not in chrome_args.launch_args(user_data_dir="/tmp/x")


def test_devtools_active_port_is_cleared_with_the_other_singletons(tmp_path: Path) -> None:
    # A stale DevToolsActivePort names the PREVIOUS run's port; a launcher that polls for
    # the file would read it as this run's and connect to a dead (or reused) port.
    for name in chrome_launcher.SINGLETON_NAMES:
        (tmp_path / name).write_text("stale")
    chrome_launcher.clear_stale_singleton(tmp_path)
    assert not any((tmp_path / name).exists() for name in chrome_launcher.SINGLETON_NAMES)
    assert "DevToolsActivePort" in chrome_launcher.SINGLETON_NAMES


def test_sandbox_retry_falls_back_once_then_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-root runtime keeps the sandbox, but if that launch fails we retry once with it
    # off (the only thing the retry changes). As root the sandbox is off from the start, so
    # the doomed sandboxed attempt never happens.
    attempts: list[bool] = []

    def fake_launch(*, no_sandbox: bool, **kwargs: Any) -> str:
        attempts.append(no_sandbox)
        if not no_sandbox:
            raise chrome_launcher.ChromeStartupError("Running as root without --no-sandbox is not supported.")
        return "chrome"

    monkeypatch.setattr(chrome_launcher, "launch", fake_launch)
    assert chrome_launcher.launch_with_sandbox_retry(no_sandbox=False, executable="x") == "chrome"
    assert attempts == [False, True]  # sandbox on (fails) -> retried off (succeeds)

    attempts.clear()
    assert chrome_launcher.launch_with_sandbox_retry(no_sandbox=True, executable="x") == "chrome"
    assert attempts == [True]  # already off: one attempt, no doomed try


def test_unclaimed_grant_passes_to_next_waiter(monkeypatch: pytest.MonkeyPatch) -> None:
    # An agent granted the browser from the resume queue but that never sends a
    # command (interrupted/killed) has its grant revoked after the claim window, so
    # the browser doesn't sit idle on a no-show.
    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", _noop_wake)
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        await browser.take_control()
        await browser.acquire("A", "Alice", wait=False, enqueue_on_busy=True)  # A queues
        await browser.return_to_agents()  # A granted + (fake) woken, but never sends a command
        assert browser._state_tuple() == ("agent", "A", False)
        assert browser._granted_at  # claim window armed (A hasn't sent a command)
        # Simulate the claim window elapsing with no command from A (lease stays older
        # than the grant -> A never claimed): the sweep revokes and frees the browser.
        overdue = time.monotonic() - bsession._CLAIM_WINDOW - 1
        browser._granted_at = overdue
        browser._lease_touched_at = overdue - 1
        assert await browser._sweep_unclaimed_grant() is True
        assert browser._state_tuple() == ("human", None, False)

    asyncio.run(go())


def test_return_to_agents_only_unpins_a_pinned_human() -> None:
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        # No-op when an agent owns it (can't yank a browser from an agent this way).
        await browser.acquire("A")
        assert await browser.return_to_agents() is False
        await browser.release("A")
        # No-op when already a free human.
        assert await browser.return_to_agents() is False
        # Un-pins a human who took control of a RESTING browser (no agent was driving, so
        # nothing is queued to resume) -> back to a free human. (Taking control FROM a
        # driving agent instead hands back to that agent; see
        # test_take_control_queues_the_displaced_owner_to_resume_first.)
        await browser.take_control()
        assert browser._resume_queue == []
        assert await browser.return_to_agents() is True
        assert browser._state_tuple() == ("human", None, False)

    asyncio.run(go())


def test_monitor_and_wait_hands_off_in_fifo_order() -> None:
    browser = _running_browser(browser_id="b2")
    order: list[tuple[str, str]] = []

    async def go() -> None:
        await browser.acquire("A", "Alice")

        async def waiter(name: str) -> None:
            order.append((name, await browser.acquire(name, name, wait=True, max_wait=5)))

        task_b = asyncio.create_task(waiter("B"))
        await asyncio.sleep(0.05)
        task_c = asyncio.create_task(waiter("C"))
        await asyncio.sleep(0.05)
        assert [w.agent_id for w in browser._wait_queue] == ["B", "C"]
        await browser.release("A")  # hands to B (first in line)
        await asyncio.sleep(0.05)
        assert browser._state_tuple() == ("agent", "B", False)
        await browser.release("B")  # hands to C
        await task_b
        await task_c
        assert browser._state_tuple() == ("agent", "C", False)
        assert order == [("B", "acquired"), ("C", "acquired")]

    asyncio.run(go())


def test_wait_times_out_and_dequeues() -> None:
    browser = _running_browser(browser_id="b3")

    async def go() -> None:
        await browser.acquire("A")
        assert await browser.acquire("Z", "Z", wait=True, max_wait=0.2) == "timed_out"
        assert browser._wait_queue == []  # a timed-out waiter removes itself

    asyncio.run(go())


def test_take_control_evicts_waiters() -> None:
    browser = _running_browser(browser_id="b4")

    async def go() -> None:
        await browser.acquire("A")

        async def waiter() -> str:
            return await browser.acquire("W", "W", wait=True, max_wait=5)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        await browser.take_control()  # preempt + pin -> waiters are evicted
        assert await task == "busy_human"
        assert browser._wait_queue == []

    asyncio.run(go())


def test_take_control_queues_the_displaced_owner_to_resume_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # A human taking control of a browser an agent is DRIVING queues that agent at the FRONT
    # of the resume queue, so it resumes first on hand-back -- even though its natural next
    # move (a read-only `state` re-check) does NOT enrol a waiter. Regression for the
    # preempted agent that was told "you're queued" while actually in no queue.
    woken: list[str] = []

    async def fake_wake(self: bsession.LiveBrowser, agent_id: str, agent_name: str | None) -> None:
        woken.append(agent_id)

    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", fake_wake)
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")  # A is driving
        await browser.take_control()  # human preempts + pins
        assert browser._state_tuple() == ("human", None, True)
        assert browser._resume_queue == [("A", "Alice")]  # A queued at the front, not dropped
        await browser.return_to_agents()  # human hands back
        assert browser._state_tuple() == ("agent", "A", False)  # granted to A synchronously
        await asyncio.sleep(0.01)  # the resume message is fire-and-forget (spawned)
        assert woken == ["A"]  # A is the one messaged to resume first

    asyncio.run(go())


def test_crash_releases_queued_agents_so_none_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    # A browser that crashes while agents are queued for it must release them all: a
    # connection-bound wait-queue waiter (task/lock) gets `crashed` instead of hanging
    # forever, and a resume-queue agent is messaged it's gone instead of waiting for a wake
    # that never comes.
    messaged: list[str] = []

    async def fake_message(self: bsession.LiveBrowser, agent_id: str, agent_name: str | None, text: str) -> None:
        messaged.append(agent_id)

    monkeypatch.setattr(bsession.LiveBrowser, "_message_agent", fake_message)
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A")  # A drives

        async def waiter() -> str:  # B: a connection-bound wait-queue waiter
            return await browser.acquire("B", "B", wait=True, max_wait=5)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        assert [w.agent_id for w in browser._wait_queue] == ["B"]
        await browser.acquire("C", "C", wait=False, enqueue_on_busy=True)  # C: resume-queue agent
        assert ("C", "C") in browser._resume_queue
        browser._on_disconnected(None)  # Chromium connection drops -> crash
        await asyncio.sleep(0.05)  # let _announce_crash reconcile the queues
        assert await task == "crashed"  # B unblocked with `crashed`, not a hang or busy_human
        assert browser._wait_queue == []
        assert browser._resume_queue == []  # C cleared
        assert "C" in messaged  # C messaged that the browser is gone

    asyncio.run(go())


def test_acquire_denied_by_human_pin_enqueues_when_requested() -> None:
    # A task/lock denied by a human pin (enqueue_on_busy=True) enrols in the resume queue so
    # it is messaged when the human hands back -- not silently dropped. (acquire returns
    # busy_human immediately because the connection-bound wait queue is only for waiting on
    # another AGENT, never on a human pin.)
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        await browser.take_control()  # human takes a free browser -> pinned, no displaced owner
        assert browser._resume_queue == []
        assert await browser.acquire("B", "B", enqueue_on_busy=True) == "busy_human"
        assert browser._resume_queue == [("B", "B")]

    asyncio.run(go())


def test_close_releases_a_queued_waiter(monkeypatch: pytest.MonkeyPatch) -> None:
    # Closing a browser (user action) must unblock a connection-bound waiter with `closed`
    # rather than leaving its task/lock hanging on a torn-down browser.
    async def fake_message(self: bsession.LiveBrowser, agent_id: str, agent_name: str | None, text: str) -> None:
        return None

    monkeypatch.setattr(bsession.LiveBrowser, "_message_agent", fake_message)
    browser = _running_browser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A")

        async def waiter() -> str:
            return await browser.acquire("B", "B", wait=True, max_wait=5)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        await browser.close()
        assert await task == "closed"
        assert browser._wait_queue == []

    asyncio.run(go())


# --- lifecycle: init -> running -> crashed -----------------------------------


def test_create_registers_init_immediately_and_returns_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fix: create() must register the browser in `init` and RETURN before the
    # (slow) Chromium launch -- the launch is kicked off as a background task. So the
    # returned session is `init`, it's already in the registry (the optimistic pane can
    # find it), and a launch task is in flight. We capture the spawned launch instead of
    # running it so the test stays Chromium-free.
    launched: list[bsession.LiveBrowser] = []
    monkeypatch.setattr(
        bsession.BrowserSessionManager, "_spawn_launch", lambda self, session, **k: launched.append(session)
    )
    mgr = bsession.BrowserSessionManager()

    async def go() -> None:
        session = await mgr.create("alex-smith")
        # Returned immediately in `init`, already registered, launch kicked off.
        assert session._lifecycle == "init"
        assert mgr.has_browser("alex-smith")
        assert launched == [session]
        # init counts toward the cap (the slot is reserved at registration).
        assert mgr.capacity()[0] == 1

    asyncio.run(go())


def test_command_on_an_init_browser_returns_starting() -> None:
    # `acquire` on a still-`init` browser is non-fatal: it returns `starting` (not an
    # error / not acquired), so the agent waits and retries rather than attaching to a
    # half-built browser. Ownership stays untouched. This is the FIRST thing an agent
    # hits, because `new` returns before Chromium is up.
    browser = bsession.LiveBrowser(browser_id="alex-smith")  # init by default

    async def go() -> None:
        assert browser.attach_url == ""  # no token until Chromium is actually up
        assert await browser.acquire("A", "Alice", wait=False) == "starting"
        assert browser._state_tuple() == ("human", None, False)
        assert browser._waiting_names() == []

    asyncio.run(go())


def test_lifecycle_init_to_running_broadcasts_the_new_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # When the background launch finishes, start() flips init -> running and broadcasts a
    # control message carrying lifecycle="running" so every viewer takes its starting
    # overlay down deterministically. We stub the heavy launch internals and assert the
    # transition + broadcast.
    casts: list[dict[str, Any]] = []
    monkeypatch.setattr(bsession.LiveBrowser, "_broadcast", lambda self, message: casts.append(message))

    browser = bsession.LiveBrowser(browser_id="alex-smith")
    assert browser._lifecycle == "init"

    async def go() -> None:
        # Drive only the tail of start() that flips the lifecycle (the rest needs real
        # Chromium); this mirrors the production transition + broadcast at the end of start.
        browser._lifecycle = "running"
        browser._broadcast(browser._control_message())
        running = [m for m in casts if m.get("type") == "control" and m.get("lifecycle") == "running"]
        assert running, "init->running must broadcast a control message with lifecycle=running"
        assert browser._is_running

    asyncio.run(go())


def test_close_broadcasts_closed_before_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Closing a browser announces `{"type": "closed"}` to every connected viewer (mirroring
    # the `crashed` broadcast) so the pane shows the terminal "terminated" overlay at once,
    # instead of flashing the loading spinner while its cast socket closes and reconnects.
    casts: list[dict[str, Any]] = []
    monkeypatch.setattr(bsession.LiveBrowser, "_broadcast", lambda self, message: casts.append(message))

    async def go() -> None:
        session = bsession.LiveBrowser(browser_id="alex-smith")
        await session.close()
        assert any(m.get("type") == "closed" and m.get("browser_id") == "alex-smith" for m in casts)

    asyncio.run(go())


def test_launch_failure_removes_the_browser_and_announces(monkeypatch: pytest.MonkeyPatch) -> None:
    # An init browser whose Chromium never comes up is REMOVED (not left as a stranded
    # init shell holding a cap slot), and a launch_failed message is broadcast so the
    # optimistic viewer pane stops retrying.
    casts: list[dict[str, Any]] = []
    monkeypatch.setattr(bsession.LiveBrowser, "_broadcast", lambda self, message: casts.append(message))

    async def boom_start(
        self: bsession.LiveBrowser, restore_tabs: list[str] | None = None, active_tab: int = 0
    ) -> None:
        raise bsession.BrowserStartupError("no CDP endpoint")

    monkeypatch.setattr(bsession.LiveBrowser, "start", boom_start)
    mgr = bsession.BrowserSessionManager()

    async def go() -> None:
        session = await mgr.create("alex-smith")
        assert mgr.has_browser("alex-smith")  # registered init
        await asyncio.gather(*list(mgr._launch_tasks))  # let the background launch run + fail
        assert not mgr.has_browser("alex-smith")  # removed, not a stranded init shell
        assert any(m.get("type") == "launch_failed" for m in casts)
        assert session._lifecycle == "init"  # the removed shell never reached running

    asyncio.run(go())


def test_create_persists_the_init_browser_before_it_is_running(monkeypatch: pytest.MonkeyPatch) -> None:
    # A browser the user just created must survive a daemon crash before its Chromium is up
    # (finding [5]): create() persists the manifest the moment the browser is registered in
    # `init`, not only after it reaches `running`. We stub the launch so it never comes up,
    # then assert the init browser is already in the on-disk manifest.
    monkeypatch.setattr(bsession.BrowserSessionManager, "_spawn_launch", lambda self, session, **k: None)
    mgr = bsession.BrowserSessionManager()

    async def go() -> None:
        session = await mgr.create("alex-smith")
        assert session._lifecycle == "init"
        # The create-time _spawn_save is fire-and-forget; let it run.
        await asyncio.gather(*list(mgr._bg_save_tasks))
        saved = manifest.read_manifest()
        assert saved is not None
        assert [e.id for e in saved.browsers] == ["alex-smith"]  # the init browser is persisted
        assert saved.browsers[0].tabs == []  # no tabs yet -> restores to home

    asyncio.run(go())


def test_failed_launch_name_is_remembered_and_cleared_on_recreate(monkeypatch: pytest.MonkeyPatch) -> None:
    # A name whose background launch FAILED is remembered (finding [7]) so the cast handler
    # can close a late/retrying optimistic viewer terminally (1008) instead of telling it to
    # retry forever. Re-registering the same name (a re-create, or a restore retry) clears
    # the memory so it stops being treated as terminally-failed.
    monkeypatch.setattr(bsession.LiveBrowser, "_broadcast", lambda self, message: None)

    async def boom_start(
        self: bsession.LiveBrowser, restore_tabs: list[str] | None = None, active_tab: int = 0
    ) -> None:
        raise bsession.BrowserStartupError("no CDP endpoint")

    monkeypatch.setattr(bsession.LiveBrowser, "start", boom_start)
    mgr = bsession.BrowserSessionManager()

    async def go() -> None:
        assert mgr.recently_failed_launch("alex-smith") is False
        await mgr.create("alex-smith")
        await asyncio.gather(*list(mgr._launch_tasks))  # launch runs + fails
        assert not mgr.has_browser("alex-smith")
        assert mgr.recently_failed_launch("alex-smith") is True  # remembered as terminal
        # Re-registering the same name supersedes the failure (no longer terminal).
        mgr._register_init_locked("alex-smith")
        assert mgr.recently_failed_launch("alex-smith") is False

    asyncio.run(go())


def test_failed_launch_memory_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    # The failed-name ring auto-evicts the oldest so it can't grow unbounded.
    monkeypatch.setattr(bsession, "_FAILED_LAUNCH_MEMORY", 2)
    mgr = bsession.BrowserSessionManager()
    # Re-create the deque so it picks up the patched maxlen (the default_factory captured the
    # old value at construction).
    mgr._failed_launch_names = deque(maxlen=2)
    mgr._failed_launch_names.append("a")
    mgr._failed_launch_names.append("b")
    mgr._failed_launch_names.append("c")  # evicts "a"
    assert mgr.recently_failed_launch("a") is False
    assert mgr.recently_failed_launch("b") is True
    assert mgr.recently_failed_launch("c") is True


class _KillableChrome:
    """Stand-in for a launched Chromium that records whether it was killed, so a test can
    assert no process handle is leaked when a launch is aborted."""

    def __init__(self) -> None:
        self.killed = False
        self.alive = True

    def kill(self) -> None:
        self.killed = True
        self.alive = False


def test_close_during_launch_does_not_resurrect_or_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    # The close()-racing-start() race: manager.close pops + closes a browser whose
    # background launch is SUSPENDED mid-start(). The launch must NOT resume to flip the
    # removed browser to "running" / broadcast a stale live state, and the Chromium it
    # already brought up must be killed (not leaked). This drives the real manager.close
    # (which awaits the in-flight launch task) and the real _abort_start_if_torn_down guard.
    casts: list[dict[str, Any]] = []
    monkeypatch.setattr(bsession.LiveBrowser, "_broadcast", lambda self, message: casts.append(message))
    started_bu = asyncio.Event()  # start() has brought up the (fake) Chromium and is suspended
    resume = asyncio.Event()      # the test lets the suspended start() proceed after closing
    launched: list[_KillableChrome] = []  # teardown clears _chrome, so hold the handle here

    async def suspending_start(
        self: bsession.LiveBrowser, restore_tabs: list[str] | None = None, active_tab: int = 0
    ) -> None:
        # Bring up a killable Chromium (as real start() does early), then suspend at an
        # await -- modelling start() parked mid-launch while close() runs. On resume, run
        # the SAME guard production uses before the flip.
        self._chrome = _KillableChrome()  # type: ignore[assignment]
        launched.append(self._chrome)  # type: ignore[arg-type]
        started_bu.set()
        await resume.wait()
        if await self._abort_start_if_torn_down():
            return
        self._lifecycle = "running"  # would-be terminal flip (must NOT be reached here)
        self._broadcast(self._control_message())

    monkeypatch.setattr(bsession.LiveBrowser, "start", suspending_start)
    mgr = bsession.BrowserSessionManager()

    async def go() -> None:
        session = await mgr.create("alex-smith")
        await started_bu.wait()  # the launch is now suspended mid-start()
        # Close concurrently: it pops the browser, then awaits the in-flight launch task.
        close_task = asyncio.create_task(mgr.close("alex-smith"))
        await asyncio.sleep(0)  # let close() pop + start awaiting the launch
        resume.set()            # now let the suspended start() resume
        await close_task
        assert launched and launched[0].killed  # Chromium killed, not leaked
        assert session._chrome is None  # ...and the handle dropped, so nothing can drive it
        assert session._lifecycle != "running"  # never flipped a removed browser to running
        assert not any(m.get("lifecycle") == "running" for m in casts)  # no stale live broadcast
        assert not mgr.has_browser("alex-smith")  # stays removed

    asyncio.run(go())


# --- manager: ids + cap ------------------------------------------------------


def test_crashed_browser_reports_crashed_to_agent_and_viewer() -> None:
    # When Chromium dies, the browser reports "crashed" to the agent's next command
    # (it doesn't try to drive a corpse), surfaces it to viewers, and shows in describe().
    browser = bsession.LiveBrowser(browser_id="b3")

    async def go() -> None:
        browser._lifecycle = "running"  # was up before Chromium died
        browser._on_disconnected()  # the keepalive poll saw the CDP client go dead
        assert browser._crashed is True and browser._lifecycle == "crashed"
        # An agent's next ownership command short-circuits to a clear "crashed" status,
        # and the token gate refuses every CDP frame -- nothing tries to drive a corpse.
        assert await browser.acquire("A", "Alice", wait=False) == "crashed"
        assert await browser._token_may_drive(browser._token) is False
        # A crashed browser must not hand out an attach URL that would drop the socket.
        assert (await browser.attach_for("A", "Alice"))["status"] == "crashed"
        # And it's reported in the fleet snapshot, with no tabs.
        desc = await browser.describe()
        assert desc["crashed"] is True and desc["tabs"] == [] and desc["lifecycle"] == "crashed"

    asyncio.run(go())


def test_intentional_close_is_not_reported_as_a_crash() -> None:
    # close() tears down the observer (which also fires `disconnected`); that's
    # expected teardown, not a crash, so _crashed must stay False.
    browser = bsession.LiveBrowser(browser_id="b3")

    async def go() -> None:
        browser._closed = True  # close() sets this before tearing down the observer
        browser._on_disconnected(None)
        assert browser._crashed is False

    asyncio.run(go())


def test_crashed_browsers_do_not_count_toward_the_fleet_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # A crashed shell lingers (to report "crashed") but must not block opening a new
    # browser, so the cap counts only live browsers.
    monkeypatch.setattr(bsession, "_MAX_SESSIONS", 2)
    mgr = bsession.BrowserSessionManager()
    live = bsession.LiveBrowser(browser_id="alex-smith")
    dead = bsession.LiveBrowser(browser_id="riley-jones")
    dead._crashed = True
    mgr._browsers["alex-smith"] = live
    mgr._browsers["riley-jones"] = dead

    async def go() -> None:
        # 1 live (init) + 1 crashed, cap 2 -> a new browser is still allowed (the crash
        # is not counted; init + running both are). Stub the background launch to a no-op
        # so create just registers the init browser without starting real Chromium.
        monkeypatch.setattr(
            bsession.BrowserSessionManager, "_spawn_launch", lambda self, *a, **k: None
        )
        result = await mgr.create("morgan-lee")
        assert result.browser_id == "morgan-lee"  # allowed despite 2 entries (one crashed)
        assert result._lifecycle == "init"  # registered, launch kicked off in the background
        assert mgr.has_browser("morgan-lee")

    asyncio.run(go())


def test_create_rejects_when_fleet_full(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cap must reject before launching Chromium, so a small compute can't be OOM-ed.
    # init browsers count toward the cap (the slot is reserved at registration), so three
    # un-launched init browsers already fill a cap of 3.
    monkeypatch.setattr(bsession, "_MAX_SESSIONS", 3)
    mgr = bsession.BrowserSessionManager()
    for name in ("a-one", "b-two", "c-three"):
        mgr._browsers[name] = bsession.LiveBrowser(browser_id=name)  # init lifecycle

    async def go() -> None:
        # The cap message surfaces the exact locked copy "3/3 browsers open -- close one first."
        with pytest.raises(bsession.FleetFullError, match=r"3/3 browsers open -- close one first\."):
            await mgr.create()

    asyncio.run(go())


def test_create_mints_the_first_free_numbered_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two create()s with no name mint browser-1 then browser-2 -- the canonical
    # forms of the "Browser N" display names the UI derives. create() registers
    # init + kicks the launch off in the background; stub the launch to a no-op
    # so the test only exercises (synchronous) name registration.
    monkeypatch.setattr(bsession.BrowserSessionManager, "_spawn_launch", lambda self, *a, **k: None)
    monkeypatch.setattr(bsession, "_MAX_SESSIONS", 5)
    mgr = bsession.BrowserSessionManager()

    async def go() -> None:
        first = await mgr.create()
        second = await mgr.create()
        assert (first.browser_id, second.browser_id) == ("browser-1", "browser-2")
        # A legacy random-named browser holds its own name without shifting the
        # numbering, and closing browser-1 frees its slot for the next create.
        mgr._browsers["alex-smith"] = bsession.LiveBrowser(browser_id="alex-smith")
        await mgr.close("browser-1")
        third = await mgr.create()
        assert third.browser_id == "browser-1"

    asyncio.run(go())


def test_create_counts_manifest_entries_and_profiles_as_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    # A saved browser that has not been restored yet (a manifest entry), and a
    # profile dir a crash orphaned, both hold their names: a minted name never
    # lands on either (which is what keeps a new browser from adopting an old
    # profile's cookies), and an explicit create naming one is refused.
    monkeypatch.setattr(bsession.BrowserSessionManager, "_spawn_launch", lambda self, *a, **k: None)
    mgr = bsession.BrowserSessionManager()
    manifest.write_manifest(manifest.Manifest(browsers=[manifest.ManifestEntry(id="browser-1", tabs=[])]))
    (bsession._PROFILE_ROOT / "browser-use-user-data-dir-browser-2").mkdir(parents=True)

    async def go() -> None:
        minted = await mgr.create()
        assert minted.browser_id == "browser-3"
        with pytest.raises(bsession.DuplicateBrowserNameError, match="saved browser"):
            await mgr.create("browser-1")
        with pytest.raises(bsession.DuplicateBrowserNameError, match="saved browser"):
            await mgr.create("browser-2")

    asyncio.run(go())


def test_create_rejects_invalid_and_duplicate_user_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bsession.BrowserSessionManager, "_spawn_launch", lambda self, *a, **k: None)
    mgr = bsession.BrowserSessionManager()

    async def go() -> None:
        created = await mgr.create("alex-smith")
        assert created.browser_id == "alex-smith"
        # A second create with the same typed name is rejected (409 at the HTTP layer).
        with pytest.raises(bsession.DuplicateBrowserNameError, match="already in use"):
            await mgr.create("alex-smith")
        # A syntactically invalid name is rejected (400 at the HTTP layer).
        with pytest.raises(bsession.InvalidBrowserNameError):
            await mgr.create("Bad Name")
        # A closed name frees up: re-creating it succeeds.
        await mgr.close("alex-smith")
        assert (await mgr.create("alex-smith")).browser_id == "alex-smith"

    asyncio.run(go())


def test_a_closed_name_is_gone_until_recreated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bsession.BrowserSessionManager, "_spawn_launch", lambda self, *a, **k: None)
    mgr = bsession.BrowserSessionManager()

    async def go() -> None:
        a = await mgr.create("alex-smith")
        assert a.browser_id == "alex-smith"
        await mgr.close("alex-smith")
        # The closed name is gone -- a command on it would 404.
        with pytest.raises(KeyError):
            mgr.get("alex-smith")

    asyncio.run(go())


def test_profile_dir_round_trips_the_name() -> None:
    # The load-bearing prefix is preserved and the suffix is exactly the name.
    path = bsession._profile_dir("alex-smith")
    assert path.name == "browser-use-user-data-dir-alex-smith"
    assert "browser-use-user-data-dir-" in path.name


# --- persistence: restore + manifest (stubbed Chromium) ----------------------
# The autouse conftest fixture redirects the profile root + manifest path to tmp.


def _stub_start(monkeypatch: pytest.MonkeyPatch, fail_names: set[str] | None = None) -> list[tuple[str, Any]]:
    """Replace LiveBrowser.start with a no-op that records (name, restore_tabs) and flips
    the lifecycle to ``running`` on success (mirroring the real start, so the manager's
    ``_launch`` treats the browser as up); names in ``fail_names`` raise
    BrowserStartupError (to test resilient restore, where ``_launch`` removes them)."""
    calls: list[tuple[str, Any]] = []

    async def fake_start(
        self: bsession.LiveBrowser, restore_tabs: list[str] | None = None, active_tab: int = 0
    ) -> None:
        calls.append((self.browser_id, restore_tabs))
        if fail_names and self.browser_id in fail_names:
            raise bsession.BrowserStartupError(f"boom {self.browser_id}")
        self._lifecycle = "running"

    monkeypatch.setattr(bsession.LiveBrowser, "start", fake_start)
    return calls


def _manager() -> bsession.BrowserSessionManager:
    mgr = bsession.BrowserSessionManager()
    return mgr


def test_restore_relaunches_saved_browsers_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_start(monkeypatch)
    manifest.write_manifest(
        manifest.Manifest(browsers=[manifest.ManifestEntry(id="alex-smith", tabs=["https://x"])])
    )
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert mgr.has_browser("alex-smith")  # restored by name


def test_restore_passes_saved_tabs_and_comes_up_resting(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_start(monkeypatch)
    manifest.write_manifest(
        manifest.Manifest(browsers=[manifest.ManifestEntry(id="riley-jones", tabs=["https://x", "https://y"])])
    )
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert ("riley-jones", ["https://x", "https://y"]) in calls  # saved tabs forwarded to start()
    restored = mgr.get("riley-jones")
    # Ownership/queues are NOT persisted: a restored browser is resting.
    assert restored._state_tuple() == ("human", None, False)
    assert restored._resume_queue == [] and restored._wait_queue == []


def test_snapshot_persists_init_running_and_crashed_topology_only() -> None:
    # The durable manifest snapshots init + running from the LIVE fleet (finding [5]: an
    # init browser the user just created must survive a daemon crash before its Chromium is
    # up). Crashed (not explicitly-closed) browsers are PRESERVED too, carried forward with
    # their last-known entry -- dropping them let the next restart sweep their profile and
    # delete every login. Only topology (id/tabs/active_tab) is persisted, never ownership.
    mgr = bsession.BrowserSessionManager()
    healthy = _running_browser("alex-smith")
    healthy.controller = "agent"  # ownership state that must NOT be persisted
    healthy.owner_agent_id = "x"
    healthy.human_pinned = True
    starting = bsession.LiveBrowser(browser_id="morgan-lee")  # init -- launch not finished
    assert starting._lifecycle == "init"
    crashed = bsession.LiveBrowser(browser_id="riley-jones")
    crashed._crashed = True
    mgr._browsers["alex-smith"] = healthy
    mgr._browsers["morgan-lee"] = starting
    mgr._browsers["riley-jones"] = crashed
    # A prior checkpoint knew riley-jones's tabs; the crashed entry is carried forward from
    # it (we can't query dead Chromium), so its profile survives and it relaunches logged in.
    mgr._last_manifest_json = bsession.fleet_manifest.Manifest(
        browsers=[bsession.fleet_manifest.ManifestEntry(id="riley-jones", tabs=["https://example.com"], active_tab=0)]
    ).model_dump_json()

    async def go() -> bsession.fleet_manifest.Manifest:
        async with mgr._lock:
            return await mgr._snapshot_manifest_locked()

    snap = asyncio.run(go())
    # init + running + crashed all persisted; crashed keeps its last-known tabs.
    assert sorted(e.id for e in snap.browsers) == ["alex-smith", "morgan-lee", "riley-jones"]
    riley = next(e for e in snap.browsers if e.id == "riley-jones")
    assert riley.tabs == ["https://example.com"]  # carried forward from the prior checkpoint
    assert set(snap.browsers[0].model_dump().keys()) == {"id", "tabs", "active_tab"}


def test_fresh_workspace_restores_to_an_empty_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    # No manifest, no profiles on disk -> NO default browser, an EMPTY fleet. Nothing
    # is launched (no browser-0 seed); the first create() opens a browser later.
    calls = _stub_start(monkeypatch)
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert calls == []  # nothing launched on a fresh workspace
    assert mgr._browsers == {}


def test_manifest_loss_with_surviving_profiles_relaunches_them(monkeypatch: pytest.MonkeyPatch) -> None:
    # No manifest, but a name-valid profile dir survived on the volume -> relaunch it
    # (tabs unknown), rather than treating this as a first boot and wiping the saved login.
    (bsession._PROFILE_ROOT / "browser-use-user-data-dir-alex-smith").mkdir(parents=True)
    calls = _stub_start(monkeypatch)
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert ("alex-smith", None) in calls and mgr.has_browser("alex-smith")


def test_legacy_numeric_profile_dirs_are_not_resurrected(monkeypatch: pytest.MonkeyPatch) -> None:
    # An upgraded workspace may have old numeric profile dirs (browser-use-user-data-dir-0).
    # is_valid_browser_name rejects pure-numeric suffixes, so they are NOT relaunched as
    # bogus "0" named browsers -- they fall through to the orphan sweep instead.
    root = bsession._PROFILE_ROOT
    (root / "browser-use-user-data-dir-0").mkdir(parents=True)
    (root / "browser-use-user-data-dir-2").mkdir(parents=True)
    calls = _stub_start(monkeypatch)
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert calls == []  # no numeric dir relaunched
    assert mgr._browsers == {}
    # And they are swept (not kept around forever as stale numeric profiles).
    assert not (root / "browser-use-user-data-dir-0").exists()
    assert not (root / "browser-use-user-data-dir-2").exists()


def test_restore_keeps_a_flaked_browser_for_next_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    # A transient relaunch failure must NOT lose the saved browser: it stays in the
    # manifest (for a next-boot retry) and its profile is NOT swept. (Durability.)
    (bsession._PROFILE_ROOT / "browser-use-user-data-dir-riley-jones").mkdir(parents=True)
    _stub_start(monkeypatch, fail_names={"riley-jones"})
    manifest.write_manifest(
        manifest.Manifest(
            browsers=[
                manifest.ManifestEntry(id="alex-smith"),
                manifest.ManifestEntry(id="riley-jones", tabs=["https://x"]),
                manifest.ManifestEntry(id="morgan-lee"),
            ],
        )
    )
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert mgr.has_browser("alex-smith") and mgr.has_browser("morgan-lee")
    assert not mgr.has_browser("riley-jones")  # flaked, not live
    reconciled = manifest.read_manifest()
    assert reconciled is not None
    entry = next((e for e in reconciled.browsers if e.id == "riley-jones"), None)
    assert entry is not None and entry.tabs == ["https://x"]  # preserved for retry
    assert (bsession._PROFILE_ROOT / "browser-use-user-data-dir-riley-jones").exists()  # NOT deleted


def test_restore_sweeps_orphan_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    root = bsession._PROFILE_ROOT
    for name in ("alex-smith", "riley-jones", "orphan-gone"):
        (root / f"browser-use-user-data-dir-{name}").mkdir(parents=True)
    _stub_start(monkeypatch)
    manifest.write_manifest(
        manifest.Manifest(
            browsers=[manifest.ManifestEntry(id="alex-smith"), manifest.ManifestEntry(id="riley-jones")]
        )
    )
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert not (root / "browser-use-user-data-dir-orphan-gone").exists()  # orphan swept
    assert (root / "browser-use-user-data-dir-alex-smith").exists()
    assert (root / "browser-use-user-data-dir-riley-jones").exists()


def test_looking_at_a_busy_browser_does_not_enqueue_the_agent() -> None:
    # Looking must not enrol the caller as a waiter. `state`'s read-only peek is gone
    # (the proxy cannot classify a CDP frame as read-only), so the non-enrolling path is
    # now a plain non-waiting acquire -- `ls`/`describe` never touch the queues at all.
    browser = _running_browser(browser_id="b0")

    async def go() -> None:
        await browser.acquire("A", "Alice")  # agent A holds it
        assert await browser.acquire("B", "Bob", wait=False, enqueue_on_busy=False) == "busy_agent"
        assert browser._waiting_names() == []  # B was NOT queued
        assert browser._resume_queue == []  # ...and not enrolled for resume either
        await browser.describe()  # a pure look enrols nothing
        assert browser._waiting_names() == [] and browser._resume_queue == []

    asyncio.run(go())


# --- ownership: the lease, now enforced per CDP frame ------------------------


def _leased(name: str = "alex-smith", agent_id: str = "A") -> bsession.LiveBrowser:
    """A running LiveBrowser with a token minted FOR ``agent_id``."""
    browser = _running_browser(browser_id=name)
    browser._mint_token(agent_id, "Alice")
    return browser


def test_the_url_new_prints_can_actually_drive_a_resting_browser() -> None:
    # The bug this pins: `run_action`'s "the first action acquires the browser" was deleted
    # with the drive verbs, and nothing replaced it -- so a freshly created browser rests
    # with the human, and the attach URL `new` printed was refused on EVERY frame. The
    # agent's first frame has to take the lease, exactly as its first command used to.
    browser = _leased("browser-1")
    assert browser.controller == "human"  # a new browser rests with the human

    async def go() -> None:
        assert await browser._token_may_drive(browser._token) is True
        assert browser._state_tuple() == ("agent", "A", False)  # the first frame acquired it
        # ...and the acquire must NOT invalidate the very URL the agent is driving with.
        assert await browser._token_may_drive(browser._token) is True

    asyncio.run(go())


def test_a_second_agent_cannot_get_a_token_for_a_held_browser() -> None:
    # A CDP client sends no identity header, so the token IS the identity. If /attach handed
    # the live token to any caller, agent B could drive agent A's browser.
    browser = _leased("browser-1", agent_id="A")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        denied = await browser.attach_for("B", "Bob")
        assert denied["ok"] is False and denied["status"] == "busy_agent"
        # The holder can always re-issue itself one (the token rotates on ownership moves).
        # The URL text needs a live ProxyServer, which the real-Chromium test covers; here
        # what matters is that a token was issued and it is bound to the right agent.
        mine = await browser.attach_for("A", "Alice")
        assert mine["ok"] is True
        assert browser._token_owner == "A"
        # A human-pinned browser refuses everyone.
        await browser.take_control()
        pinned = await browser.attach_for("A", "Alice")
        assert pinned["ok"] is False and pinned["status"] == "busy_human"

    asyncio.run(go())


def test_token_gate_is_the_per_frame_replacement_for_the_command_cas() -> None:
    # `run_action`'s compare-and-set used to re-check ownership right before each verb.
    # Driving is now raw CDP, so the same check moved into the proxy's per-frame gate:
    # a token that no longer matches the lease holder cannot move the browser.
    browser = _leased()

    async def go() -> None:
        await browser.acquire("A", "Alice")
        token = browser._token
        assert await browser._token_may_drive(token) is True
        # A stale/absent token is refused outright (this is how agent B is kept out --
        # a generic CDP client sends no x-mngr-agent-id header, so the token is the
        # ONLY thing distinguishing one attacher from another).
        assert await browser._token_may_drive("not-the-token") is False
        assert await browser._token_may_drive("") is False
        # A human take-control makes the very next frame fail, mid-session.
        await browser.take_control()
        assert await browser._token_may_drive(token) is False

    asyncio.run(go())


def test_the_token_survives_its_own_agent_but_not_another(monkeypatch: pytest.MonkeyPatch) -> None:
    # Rotation is scoped deliberately. The moment-to-moment guarantee comes from the
    # per-frame lease check, not from re-minting -- the token is identity, not authority --
    # so it must survive everything except the browser genuinely changing hands to someone
    # else. See the takeover test for why re-minting too eagerly breaks resumption.
    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", _noop_wake)
    browser = _leased("riley-jones", agent_id="A")

    async def go() -> None:
        first = browser._token
        await browser.acquire("A", "Alice")
        assert browser._token == first  # its OWN agent acquiring must not invalidate it
        await browser.take_control()
        assert browser._token == first  # nor may a human takeover (the socket must survive)
        assert await browser._token_may_drive(first) is False  # ...but it cannot drive
        # The human hands back; A is at the front of the resume queue, so it lands on A.
        await browser.return_to_agents()
        await browser.release("A")
        assert await browser.acquire("B", "Bob", wait=False) == "acquired"
        assert browser._token != first  # a DIFFERENT agent does invalidate it
        assert await browser._token_may_drive(first) is False

    asyncio.run(go())


def test_a_forwarded_frame_touches_the_lease_but_an_idle_socket_does_not() -> None:
    # An ATTACHED-but-silent CDP session looks identical to an abandoned one at the
    # socket layer, so only a forwarded FRAME counts as activity. Otherwise a session
    # left open would pin a browser away from the human forever.
    browser = _leased("morgan-lee")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        browser._lease_touched_at = time.monotonic() - (bsession._LEASE_IDLE_TTL + 10)
        # Merely holding the socket open changes nothing...
        assert await browser._sweep_idle_lease() is True
        assert browser._state_tuple() == ("human", None, False)

    asyncio.run(go())


def test_idle_lease_sweep_releases_only_a_quiet_lease() -> None:
    browser = _leased("jordan-kim")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        # Fresh lease -> not swept.
        assert await browser._sweep_idle_lease() is False
        assert browser._state_tuple() == ("agent", "A", False)
        # Quiet past the TTL -> released back to the human. 60s, not 90s: _LEASE_IDLE_TTL
        # was lowered deliberately and the docs lagged behind it.
        assert bsession._LEASE_IDLE_TTL == 60
        browser._lease_touched_at = time.monotonic() - (bsession._LEASE_IDLE_TTL + 10)
        assert await browser._sweep_idle_lease() is True
        assert browser._state_tuple() == ("human", None, False)
        # A forwarded frame is what keeps it alive.
        await browser.acquire("A", "Alice")
        browser._lease_touched_at = time.monotonic() - (bsession._LEASE_IDLE_TTL + 10)
        browser.touch_lease()
        assert await browser._sweep_idle_lease() is False

    asyncio.run(go())


# --- cast fan-out: outbound queue per socket (the Flask<->loop WS inversion) ---


def test_register_cast_queue_seeds_initial_control() -> None:
    # A freshly-registered cast queue is seeded with the current control state as its
    # FIRST message, so the viewer's first message is deterministic. The control seed
    # carries the lifecycle (here `init` -- the browser hasn't launched), so the viewer
    # shows the starting overlay until it sees `running`. /cast carries only control now
    # (no pixels, no tab list), so nothing else is seeded on a non-crashed browser.
    browser = bsession.LiveBrowser(browser_id="b1")  # init by default

    async def go() -> None:
        q = await browser.register_cast_queue()
        first = _pop_json(q)
        assert first["type"] == "control" and first["owner"] == "human"
        assert first["lifecycle"] == "init"  # the viewer renders the starting overlay off this
        assert q.empty()  # not crashed -> only the control seed
        assert q in browser._cast_queues

    asyncio.run(go())


def test_register_cast_queue_seeds_crash_state_when_crashed() -> None:
    # Pixels ride the pixelflux /stream socket now (seeded there with a fresh IDR on
    # connect), not the cast queue -- so register_cast_queue seeds only control (+ crashed
    # when the browser is dead). A crashed browser seeds the crash state and no frame.
    browser = bsession.LiveBrowser(browser_id="b1")
    browser._crashed = True

    async def go() -> None:
        q = await browser.register_cast_queue()
        assert _pop_json(q)["type"] == "control"
        assert _pop_json(q)["type"] == "crashed"
        assert q.empty()  # crashed -> crash state, never a frame

    asyncio.run(go())


def test_register_cast_queue_with_lifecycle_returns_the_browsers_lifecycle() -> None:
    # The runner reads the lifecycle alongside the new queue (same on-loop step) so it can
    # decide whether to push the fleet-level `initializing` banner: a viewer joining an
    # already-running browser must NOT be told it's initializing (finding [3-runner]).
    running = _running_browser(browser_id="b1")
    starting = bsession.LiveBrowser(browser_id="b2")  # init

    async def go() -> None:
        _q, lifecycle = await running.register_cast_queue_with_lifecycle()
        assert lifecycle == "running"
        _q2, lifecycle2 = await starting.register_cast_queue_with_lifecycle()
        assert lifecycle2 == "init"

    asyncio.run(go())


def test_broadcast_fans_out_to_registered_queues_and_unregister_removes() -> None:
    browser = bsession.LiveBrowser(browser_id="b1")

    async def go() -> None:
        q = await browser.register_cast_queue()
        # Drain the initial seed so we only see the broadcast below.
        while not q.empty():
            q.get_nowait()
        browser._broadcast({"type": "frame", "data": "abc"})
        msg = _pop_json(q)
        assert msg == {"type": "frame", "data": "abc"}
        # Unregister stops further fan-out to this queue.
        await browser.unregister_cast_queue(q)
        assert q not in browser._cast_queues
        browser._broadcast({"type": "frame", "data": "def"})
        assert q.empty()

    asyncio.run(go())


def test_broadcast_drops_oldest_frame_when_a_slow_client_queue_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    # A client that falls behind must not block the loop: _broadcast drops the OLDEST
    # buffered frame and enqueues the newest (only the latest frame matters).
    monkeypatch.setattr(bsession, "_CAST_QUEUE_MAX_SIZE", 2)
    browser = bsession.LiveBrowser(browser_id="b1")

    async def go() -> None:
        q = await browser.register_cast_queue()
        while not q.empty():
            q.get_nowait()
        for n in range(5):
            browser._broadcast({"type": "frame", "data": str(n)})
        # maxsize 2 -> only the two most-recent frames survive (3 and 4).
        survivors = []
        while not q.empty():
            survivors.append(_pop_json(q)["data"])
        assert survivors == ["3", "4"]

    asyncio.run(go())


def test_orphaned_chromium_is_reaped_before_a_second_one_launches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Pre-existing bug this closes: an UNEXPECTED `[program:browser]` restart leaves Chromium
    # orphaned (supervisord's stopasgroup only covers a deliberate stop). The restore path then
    # cleared the singleton locks the still-running browser held and launched a SECOND Chromium
    # onto the same user_data_dir -- two writers, one profile. The orphan is also invisible to
    # OOM retagging (it is no longer our descendant), so under pressure earlyoom sheds the agent
    # before the browser: the exact inversion oom_retag exists to prevent.
    killed: list[int] = []
    holders = [4242, 4242, None]  # alive, still alive after SIGTERM check, then gone

    monkeypatch.setattr(chrome_launcher, "profile_holder_pid", lambda _d: holders.pop(0) if holders else None)
    monkeypatch.setattr(chrome_launcher.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(chrome_launcher.time, "sleep", lambda _s: None)

    assert chrome_launcher.reap_orphan(tmp_path) is True
    assert killed == [4242]  # signalled the orphan rather than launching alongside it


def test_reap_orphan_is_a_noop_when_no_one_holds_the_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(chrome_launcher, "profile_holder_pid", lambda _d: None)
    assert chrome_launcher.reap_orphan(tmp_path) is False


def test_profile_holder_probe_terminates_pgrep_options(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # `pgrep -f` read the leading `--` of `--user-data-dir=...` as an option and silently
    # matched NOTHING, which made the orphan guard a no-op. `--` terminates option parsing.
    # Only a live run against a real browser caught this; no unit test could have.
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: object) -> Any:
        seen.append(argv)
        return type("R", (), {"stdout": ""})()

    monkeypatch.setattr(chrome_launcher.subprocess, "run", fake_run)
    chrome_launcher.profile_holder_pid(tmp_path)
    assert seen and seen[0][:3] == ["pgrep", "-f", "--"], seen


def test_pane_follow_never_reasserts_a_stale_cached_tab() -> None:
    # A human switches tabs inside Chrome via XTEST, which never reaches our CDP
    # connection -- so `_active_target_id` still names the tab the AGENT was on. Falling
    # back to it on the agent's next frame yanked the human off the tab they had chosen.
    # Foreground only a target the agent actually named.
    browser = _running_browser(browser_id="browser-1")
    browser._active_target_id = "AGENTS-OLD-TAB"
    activated: list[str] = []

    async def fake_focus(self: bsession.LiveBrowser, target_id: str) -> None:
        activated.append(target_id)

    async def go() -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bsession.LiveBrowser, "_focus_and_foreground", fake_focus)
            await browser._on_proxy_activity(None)  # frame resolved to no target
            assert activated == [], "must not re-assert the cached tab"
            await browser._on_proxy_activity("THE-TAB-THE-AGENT-IS-ON")
            assert activated == ["THE-TAB-THE-AGENT-IS-ON"]

    asyncio.run(go())


def test_a_human_takeover_does_not_kill_the_agents_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    # THE takeover flow: the agent's live socket carries the token it attached with, so
    # re-minting on takeover would kill it permanently -- and re-attaching is not a way out,
    # because a playwright-cli slug is poisoned once its session is torn down. Handing
    # control back has to leave the agent able to carry on.
    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", _noop_wake)
    browser = _leased("browser-1", agent_id="A")

    async def go() -> None:
        attached_with = browser._token
        assert await browser._token_may_drive(attached_with) is True
        await browser.take_control()
        assert await browser._token_may_drive(attached_with) is False  # refused while held
        await browser.return_to_agents()
        assert await browser._token_may_drive(attached_with) is True  # ...and resumes after

    asyncio.run(go())


def test_a_different_agent_taking_the_browser_does_kill_the_old_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # The rotation that DOES matter: otherwise the previous holder keeps driving.
    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", _noop_wake)
    browser = _leased("browser-1", agent_id="A")

    async def go() -> None:
        a_token = browser._token
        await browser.acquire("A", "Alice")
        await browser.release("A")
        await browser.acquire("B", "Bob")
        assert browser._token != a_token
        assert await browser._token_may_drive(a_token) is False

    asyncio.run(go())


def test_launch_args_declare_english_explicitly() -> None:
    # The container has no LANG/LC_ALL, so without these Chrome's language is whatever the
    # base image happens to imply. `--accept-lang` sets the header outright, which is what
    # makes it independent of the container locale.
    args = chrome_args.launch_args(user_data_dir="/tmp/lang-check")
    assert "--lang=en-US" in args
    assert "--accept-lang=en-US,en" in args


def test_a_new_browser_lands_on_a_blank_page() -> None:
    # It used to land on google.com, so the first thing anyone saw on a new browser was
    # Google's consent interstitial -- in French, because Google decides both the language
    # and the "this looks like the EU" question from IP geolocation, and our egress is an
    # OVH range registered in Roubaix. `?hl=en` would only have translated that wall; the
    # interstitial appears because of WHERE Google thinks we are, which a language
    # parameter does not change. A blank page has nothing to geolocate.
    assert bsession._HOME_URL == "about:blank"
    # ...and it must not be persisted as a restorable tab, or every restart would reopen it.
    assert bsession._is_restorable_url(bsession._HOME_URL) is False


# --- the shell nudge (every fleet event the instances API's status derives from) ---


def test_every_ownership_write_nudges_the_shell_once() -> None:
    browser = _running_browser(browser_id="b1")
    nudger = RecordingNudger()
    browser._nudger = nudger

    async def go() -> None:
        await browser.acquire("A", "Alice")
        assert nudger.nudge_count == 1
        # The same agent re-acquiring writes nothing, so it tells the shell nothing.
        await browser.acquire("A", "Alice")
        assert nudger.nudge_count == 1
        await browser.release("A")
        assert nudger.nudge_count == 2
        await browser.take_control()
        assert nudger.nudge_count == 3
        await browser.return_to_agents()
        assert nudger.nudge_count == 4

    asyncio.run(go())


def test_a_crash_nudges_the_shell_once() -> None:
    browser = _running_browser(browser_id="b1")
    nudger = RecordingNudger()
    browser._nudger = nudger

    browser._crashed = True
    browser._crashed = True

    assert nudger.nudge_count == 1


def test_registering_and_closing_a_browser_nudge_the_shell_and_hand_it_the_nudger() -> None:
    mgr = bsession.BrowserSessionManager()
    nudger = RecordingNudger()
    mgr.set_nudger(nudger)

    registered = mgr._register_init_locked("browser-1")

    assert nudger.nudge_count == 1
    assert registered._nudger is nudger
    asyncio.run(mgr.close("browser-1"))
    assert nudger.nudge_count == 2
    asyncio.run(mgr.close("browser-1"))  # an unknown name changes nothing
    assert nudger.nudge_count == 2


def test_set_nudger_reaches_browsers_registered_before_it() -> None:
    mgr = bsession.BrowserSessionManager()
    registered = mgr._register_init_locked("browser-1")
    nudger = RecordingNudger()

    mgr.set_nudger(nudger)
    registered._crashed = True

    assert nudger.nudge_count == 1


# --- the bridged fleet (the instances adapter's verbs, run on the daemon's loop) ---


def _bridged_fleet(manager: bsession.BrowserSessionManager, route_timeout_seconds: float) -> BridgedFleet:
    return BridgedFleet(
        bridge=runner.bridge,
        manager=manager,
        ready_gate=runner._init_done,
        route_timeout_seconds=route_timeout_seconds,
    )


def test_bridged_fleet_answers_a_daemon_failure_under_a_verb_as_unavailable() -> None:
    async def fail_to_start() -> None:
        raise bsession.BrowserStartupError("no CDP endpoint")

    with pytest.raises(FleetUnavailableError, match="no CDP endpoint") as caught:
        _bridged_fleet(bsession.BrowserSessionManager(), route_timeout_seconds=5)._run_on_loop(fail_to_start())

    assert isinstance(caught.value.__cause__, bsession.BrowserStartupError)


def test_bridged_fleet_answers_a_stalled_loop_as_unavailable() -> None:
    async def outlast_the_route() -> None:
        await asyncio.sleep(3600)

    with pytest.raises(FleetUnavailableError, match="could not complete"):
        _bridged_fleet(bsession.BrowserSessionManager(), route_timeout_seconds=0.05)._run_on_loop(outlast_the_route())


def test_bridged_fleet_passes_the_fleets_own_refusal_through() -> None:
    async def refuse() -> None:
        raise bsession.FleetFullError("2/2 browsers open -- close one first.")

    with pytest.raises(bsession.FleetFullError, match="close one first"):
        _bridged_fleet(bsession.BrowserSessionManager(), route_timeout_seconds=5)._run_on_loop(refuse())


def test_bridged_fleet_create_refuses_a_full_fleet_with_the_daemons_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    # Skip the install check (there is no Chromium here) and fill the cap with un-launched
    # init browsers: the cap rejects before anything registers, so nothing launches.
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")
    mgr = bsession.BrowserSessionManager()
    for idx in range(bsession._MAX_SESSIONS):
        mgr._browsers[f"browser-{idx + 1}"] = bsession.LiveBrowser(browser_id=f"browser-{idx + 1}")

    with pytest.raises(FleetCreateRefusedError, match="close one first") as caught:
        _bridged_fleet(mgr, route_timeout_seconds=5).create_browser()

    assert isinstance(caught.value.__cause__, bsession.FleetFullError)
    assert len(mgr._browsers) == bsession._MAX_SESSIONS


def test_create_snapshot_reports_the_new_browser_as_launching() -> None:
    mgr = bsession.BrowserSessionManager()

    async def go() -> BrowserSnapshot:
        snapshot = await mgr.create_snapshot()
        # The launch was only scheduled; cancel it before it gets a turn, so no Chromium starts.
        for launch in mgr._launch_tasks:
            launch.cancel()
        return snapshot

    snapshot = asyncio.run(go())

    assert snapshot == BrowserSnapshot(
        name=BrowserName("browser-1"),
        lifecycle=BrowserLifecycle.INIT,
        controller=BrowserController.HUMAN,
    )
    assert mgr.has_browser("browser-1")


# --- the location verb (navigate the active tab, then checkpoint the manifest) ---


def _page(target_id: str, url: str) -> dict[str, Any]:
    return {"targetId": target_id, "url": url, "type": "page"}


def test_navigate_browser_points_the_active_tab_at_the_url_and_checkpoints_the_manifest() -> None:
    mgr = bsession.BrowserSessionManager()
    browser = _running_browser(browser_id="browser-1")
    cdp = NavigatingCdpClient(
        targets=[_page("t1", "https://first.example/"), _page("t2", "https://second.example/")],
        navigation_failure=None,
    )
    browser._cdp = cdp
    browser._active_target_id = "t2"
    mgr._browsers["browser-1"] = browser

    async def go() -> None:
        await mgr.navigate_browser("browser-1", "https://new.example/page")
        # The checkpoint is fire-and-forget on the loop; let it land before the loop closes.
        await asyncio.gather(*mgr._bg_save_tasks)

    asyncio.run(go())

    assert cdp.navigations == [("t2", "https://new.example/page")]
    assert browser._active_target() == "t2"
    saved = manifest.read_manifest()
    assert saved is not None
    assert [(entry.id, entry.tabs, entry.active_tab) for entry in saved.browsers] == [
        ("browser-1", ["https://first.example/", "https://new.example/page"], 1)
    ]


def test_navigate_active_tab_falls_back_to_the_first_page_when_none_was_foregrounded() -> None:
    browser = _running_browser(browser_id="browser-1")
    cdp = NavigatingCdpClient(
        targets=[_page("t1", "about:blank"), _page("t2", "https://second.example/")],
        navigation_failure=None,
    )
    browser._cdp = cdp

    asyncio.run(browser.navigate_active_tab("https://new.example/"))

    assert cdp.navigations == [("t1", "https://new.example/")]
    assert browser._active_target() == "t1"


def test_navigate_active_tab_reports_a_refused_navigation_and_a_tabless_browser_as_failed() -> None:
    refusing = _running_browser(browser_id="browser-1")
    refusing._cdp = NavigatingCdpClient(
        targets=[_page("t1", "about:blank")], navigation_failure="net::ERR_NAME_NOT_RESOLVED"
    )
    tabless = _running_browser(browser_id="browser-2")
    tabless._cdp = NavigatingCdpClient(targets=[], navigation_failure=None)

    with pytest.raises(NavigationFailedError, match="ERR_NAME_NOT_RESOLVED"):
        asyncio.run(refusing.navigate_active_tab("https://nowhere.invalid/"))
    with pytest.raises(NavigationFailedError, match="no tab to navigate"):
        asyncio.run(tabless.navigate_active_tab("https://example.com/"))
    assert refusing._active_target() is None
