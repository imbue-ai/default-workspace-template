"""Live browser fleet: headful Chromium + a display-captured live view + a per-browser ownership state machine.

Each :class:`LiveBrowser` owns one headful Chromium (launched and driven by
``browser_use.BrowserSession``) running under its OWN X display (see
:mod:`browser.display`), plus a second, observer-only Playwright connection over
the same CDP endpoint. The human live view is a striped H.264/JPEG capture of that
display (:mod:`browser.capture`, streamed over a dedicated ``/stream`` WebSocket and
decoded with WebCodecs), and the human's mouse/keyboard is injected at the DISPLAY
level via XTest -- so native right-click menus, ``<select>`` dropdowns, and drag all
work. The browser-use side does the AI driving (``Agent.run``) over CDP, untouched.
Both share the one Chromium, so the human sees exactly what the agent does and vice
versa. (Headless -- tests / bare dev boxes -- skips the display, capture, and XTest;
only the agent's CDP control is available there.)

Ownership is a small per-browser state machine, and it is the heart of this
module. Many agents (a chat agent plus its sub-agents, each a distinct
``MNGR_AGENT_ID``) share one fleet; any single browser is controlled by exactly
one party at a time: a specific agent, or the human. Every control change goes
through the single writer :meth:`LiveBrowser._write_control_locked`, called only
under ``_control_lock`` with a compare-and-set guard, so there is no bespoke
ordering anywhere and "single asyncio process" actually means atomic. The state:

* ``controller`` -- ``"human"`` or ``"agent"``.
* ``owner_agent_id`` -- which agent holds it (when ``controller == "agent"``).
* ``human_pinned`` -- the human explicitly took control; agents are locked out
  until the human hands back. (Idle ``human`` with ``human_pinned`` false is
  the resting state: human-drivable and agent-acquirable.)

Rules that fall out of this and never need a special case:

* Agents NEVER preempt anyone. :meth:`acquire` succeeds only against an unpinned
  human (or the same agent re-acquiring); against another agent it parks the
  caller in a FIFO wait-queue (monitor-and-wait) until that agent releases.
* The human ALWAYS wins: :meth:`take_control` preempts whatever agent is driving
  (cancelling its run) and pins; a pinned browser evicts any waiters.
* Ownership is bound to the live ``task`` request (see runner.py): if the agent
  process dies, the request disconnects, the run is cancelled, and the browser
  is released -- no fire-and-forget locks, no stuck owners.

The Anthropic API key is read lazily from the environment (and a fresh re-read of
``$MNGR_HOST_DIR/env``) at run time, so a key submitted after this service booted
is still picked up without a restart. Direct Anthropic API key only -- the Imbue
Cloud / litellm proxy (``ANTHROPIC_BASE_URL``) path is intentionally unsupported.
"""

import asyncio
import base64
import json
import os
import queue
import shutil
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any, Literal

from browser_use import Agent, BrowserSession, ChatAnthropic
from browser_use.skill_cli.actions import ActionHandler
from imbue.imbue_common.mutable_model import MutableModel
from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from pydantic import PrivateAttr

from browser import manifest as fleet_manifest
from browser.audio import AudioCapture, create_null_sink, remove_null_sink
from browser.capture import Capture
from browser.display import SCREEN_H, SCREEN_W, Display, DisplayError
from browser.names import generate_browser_name, is_valid_browser_name
from browser.oom_retag import notify_chromium_processes_expected

# browser-use phones home anonymized telemetry by default; disable it (the
# compute has no business making that call, and it spams connection-error logs
# where egress is restricted). setdefault so an explicit opt-in still wins.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

# Errors expected when a page/target/CDP session goes away underneath us (tab
# closed, navigation, browser killed). PlaywrightError covers TargetClosedError.
_BROWSER_ERRORS = (RuntimeError, ConnectionError, OSError, PlaywrightError)

ControlOwner = Literal["human", "agent"]

# Explicit per-browser lifecycle. A browser is REGISTERED in the fleet the instant
# create() is called (so the viewer/CLI can address it at once), but its Chromium is
# launched asynchronously and serialized, so it starts in ``init`` and flips to
# ``running`` only once Chromium is up and the screencast is attached. ``crashed`` is
# terminal (Chromium died -- OOM/segfault). Driving/ownership only applies once
# ``running``; the viewer renders deterministically off this field.
Lifecycle = Literal["init", "running", "crashed"]

# An event emitted by a running task: thinking / action / status / done / error /
# preempted. The runner streams these to the agent's CLI as line-delimited JSON.
TaskEvent = dict[str, Any]
EventSink = Callable[[TaskEvent], Awaitable[None]]

# The live render size floats between a floor (smaller reflows sites to mobile
# layouts) and a cap: the viewer reports its pane size and we resize the browser
# WINDOW to fill it -- never the Xvfb framebuffer, which cannot grow past its initial
# size. Up to _MAX_SESSIONS browsers render concurrently, so the cap bounds memory.
_RENDER_DEFAULT_WIDTH = 1280
_RENDER_DEFAULT_HEIGHT = 800
# Cap the window/capture (== encoded) size at the Xvfb framebuffer. The viewer canvas
# CSS-stretches to fill its pane, so a bigger pane/fullscreen fills WITHOUT rendering
# bigger -- it upscales from here -- keeping the encoder cost bounded no matter the monitor.
_RENDER_MAX_WIDTH = SCREEN_W
_RENDER_MAX_HEIGHT = SCREEN_H
# Floor for the clamp -- small enough that a typical (sub-1280) panel actually
# tracks its size instead of pinning to a too-big minimum, but not degenerate.
_RENDER_MIN_WIDTH = 640
_RENDER_MIN_HEIGHT = 480

# Viewer mouse button name -> X button number (XTest). Wheel is handled separately
# (buttons 4-7). "none" (a bare move) never presses a button.
_MOUSE_BUTTONS = {"left": 1, "middle": 2, "right": 3, "none": 0}

# Fortress's fixed install path (see the env.d unit
# system/scripts/env.d/1000-playwright-fortress.sh). A stealth, C++-patched
# Chromium fork -- replaces vanilla Chromium as the engine for every browser
# the fleet launches. It installs asynchronously on first container boot via
# the env-converge one-shot; launching a browser before it exists fails, so
# callers gate on the binary itself (the unit's own satisfied condition --
# there are no marker files). The headful display/audio runtime (Xvfb, xclip,
# PulseAudio, pixelflux's libva/libgbm libs) installs the same way via
# system/scripts/env.d/1010-browser-display-audio.sh, gated on the Xvfb binary.
_FORTRESS_EXECUTABLE = "/opt/fortress/tilion-fortress/tilion"

# Default model. browser-use's own default LLM is ChatBrowserUse (its hosted
# model), so to drive with the user's Anthropic key we pass ChatAnthropic
# explicitly. Overridable via env for easy iteration; the string is sent to the
# API as-is (browser-use accepts an arbitrary model string).
_DEFAULT_MODEL = os.environ.get("BROWSER_USE_MODEL", "claude-sonnet-4-6")

# Headless by DEFAULT (safe for tests / CI / bare dev boxes with no X server): only
# the agent's CDP control is available. The workspace's supervisord runs the fleet
# HEADFUL by setting BROWSER_HEADLESS=0 -- then each browser gets its OWN per-browser
# Xvfb (see browser.display), the human view is a striped H.264/JPEG capture of that
# display (browser.capture), and human input is injected at the display level via
# XTest. Per-browser displays also mean browsers never share an X11 clipboard.
_HEADLESS = os.environ.get("BROWSER_HEADLESS", "1") != "0"

# Page the browser opens on, and the default for "New tab".
_HOME_URL = os.environ.get("BROWSER_HOME_URL", "https://www.google.com")

# Server-side cast keepalive: a static page emits no screencast frames, so without
# traffic the system_interface WS proxy closes the idle stream (~30s). A periodic
# ping keeps the backend->client direction alive between real frames.
_KEEPALIVE_SECONDS = 10

# How often to poll a watched browser's X clipboard for a copy that happened inside
# the remote page (right-click -> Copy), to push to the user's local clipboard.
_CLIPBOARD_POLL_SECONDS = float(os.environ.get("BROWSER_CLIPBOARD_POLL_SECONDS", "1.0"))

# Hard cap on any xclip subprocess. The clipboard paths hold _control_lock while running
# xclip, so a hung xclip (a wedged X server) would otherwise stall every acquire /
# take-control / sweep for that browser. On timeout we kill the child and treat it as a
# clipboard error.
_XCLIP_TIMEOUT = float(os.environ.get("BROWSER_XCLIP_TIMEOUT", "5"))


async def _communicate_bounded(proc: "asyncio.subprocess.Process", data: bytes | None = None) -> tuple[bytes, bytes]:
    """``proc.communicate(data)`` with a hard timeout; kills the child if it overruns so a
    wedged xclip can't hold a lock forever. Raises TimeoutError on overrun."""
    try:
        return await asyncio.wait_for(proc.communicate(data), timeout=_XCLIP_TIMEOUT)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise

# Outbound buffer depth per cast WebSocket. Screencast frames are produced on the
# loop and drained by a Flask thread; if a client falls behind we drop the OLDEST
# frame (a stale frame is worthless -- only the latest matters) rather than block
# the loop. A handful of frames is plenty of slack for a momentarily-slow client.
_CAST_QUEUE_MAX_SIZE = 16

# Each live session = a headless Chromium + a Playwright observer; cap the concurrent
# count so a small compute (e.g. 4 GB) can't be OOM-ed. Override via BROWSER_MAX_SESSIONS.
_MAX_SESSIONS = int(os.environ.get("BROWSER_MAX_SESSIONS", "3"))

# Names whose background launch FAILED are remembered briefly so a late/retrying optimistic
# viewer (still in 1013 reconnect-backoff when the launch failed, so it never registered a
# cast queue and missed the launch_failed broadcast) is closed terminally (1008) instead of
# told "try again" forever. A small ring is plenty: the cap is 3, and an entry only needs to
# outlive a viewer's reconnect backoff (a few seconds). See BrowserSessionManager.
_FAILED_LAUNCH_MEMORY = int(os.environ.get("BROWSER_FAILED_LAUNCH_MEMORY", "32"))

# Hard ceilings on a single browser-use task so a hung or non-cancel-safe run can
# never pin a browser forever (the connection-disconnect path is the primary
# release; these are the backstop). Both env-tunable.
_TASK_MAX_STEPS = int(os.environ.get("BROWSER_TASK_MAX_STEPS", "100"))
_TASK_MAX_SECONDS = float(os.environ.get("BROWSER_TASK_MAX_SECONDS", "900"))

# Direct-control ownership is a STICKY LEASE: an agent acquires a browser on its
# first command and holds it across subsequent commands. Unlike a `task` (whose
# ownership is bound to the long run), a lease has no live connection to detect a
# dead/wandered-off owner, so it auto-releases after this many seconds with no
# command (the keepalive loop sweeps it). The human take-control is the instant
# escape hatch; this TTL is the backstop. Env-tunable.
_LEASE_IDLE_TTL = float(os.environ.get("BROWSER_LEASE_IDLE_TTL", "90"))

# A human take-control is STICKY: it blocks agents until the human explicitly hands
# back ("Return to agent"). There is no idle/grace yield -- a human who grabs a
# browser keeps it even if they walk away mid-CAPTCHA/login, so they never come back
# to find an agent moved the page out from under them. (Agents still auto-release via
# _LEASE_IDLE_TTL; the asymmetry is deliberate -- a dead agent must not hoard, a human
# must not be force-yielded.)

# When the browser frees and is handed to a queued agent, that agent is *messaged*
# to resume (it ended its turn). If it doesn't actually take the wheel (send a
# command) within this window -- e.g. it was interrupted/killed -- the grant is
# revoked and the browser passes to the next waiter, instead of sitting idle for
# the full _LEASE_IDLE_TTL on a no-show.
_CLAIM_WINDOW = float(os.environ.get("BROWSER_CLAIM_WINDOW", "12"))

# Chromium's in-process sandbox cannot run as root: it exits with "Running as root
# without --no-sandbox is not supported" (crbug 638180), and browser-use swallows that
# into a ~30s launch hang. Every minds workspace runs this daemon as ROOT inside an OUTER
# boundary -- gVisor (runsc) under docker/cloud/AWS, the VM under Lima/Vultr -- so the
# inner sandbox is both unusable-as-root and redundant. We therefore disable it whenever
# we're root (the reliable signal; browser-use's own IN_DOCKER check misses the bare-VM
# Lima case, since Lima is a VM, not a container), and keep it for a non-root runtime
# (e.g. local dev) where it works and there may be no outer boundary. BROWSER_NO_SANDBOX=1
# forces it off regardless.
_NO_SANDBOX = os.environ.get("BROWSER_NO_SANDBOX", "").strip().lower() in ("1", "true", "yes", "on")


def _should_disable_sandbox() -> bool:
    """Whether to launch Chromium with its sandbox off: forced via BROWSER_NO_SANDBOX, or
    running as root (where Chromium refuses to start the sandbox). See _NO_SANDBOX."""
    return _NO_SANDBOX or os.geteuid() == 0


def _repo_root() -> Path:
    """The workspace root (where ``system/scripts/`` lives), anchored on this file's location
    rather than cwd -- used as the wake subprocess's cwd so the ``mngr`` dev shim
    resolves this checkout regardless of where the daemon was started."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "system" / "scripts").is_dir() and (candidate / "system" / "libs").is_dir():
            return candidate
    return Path.cwd()


# Where `screenshot` writes PNGs (relative to the daemon's cwd = repo root). The
# CLI prints the path and the agent reads the file; agent + daemon share the FS.
_SCREENSHOT_DIR = Path(os.environ.get("BROWSER_SCREENSHOT_DIR", "data/.state/browser-screenshots"))

# Sentinel the fleet wraps its agent-facing nudges in before sending them via
# `mngr message` (see `_message_agent`). These nudges land in the agent's
# transcript as an ordinary user turn; without a marker the system_interface
# transcript UI shows them as a bare user bubble, as if the human had typed
# them. Wrapping lets that UI recognise the message and render it as a collapsed
# system chip instead (like Stop-hook feedback).
#
# CROSS-LAYER CONTRACT: the reading side is the frontend's `BROWSER_FLEET_TAG` in
# system/libs/system_interface/frontend/src/views/message-kinds.ts -- keep the tag in
# sync. We wrap here in the fleet's OWN service (not in mngr, which is an
# independent product with no stake in this display concern). The wrapper adds no
# newlines, so a wrapped message types into the agent's pane identically to the
# same text sent unwrapped.
_SYSTEM_MESSAGE_TAG = "agentic-browser-fleet"


def _wrap_system_message(text: str) -> str:
    """Wrap an automated agent-facing nudge in the ``_SYSTEM_MESSAGE_TAG`` sentinel
    (see its comment). Adds no newlines, so the wrapped text types into the agent's
    pane identically to ``text`` sent unwrapped."""
    return f"<{_SYSTEM_MESSAGE_TAG}>{text}</{_SYSTEM_MESSAGE_TAG}>"

# Per-browser persistent Chromium profiles (cookies/logins/history) live here, on the
# workspace volume under $MNGR_HOST_DIR -- Tier A durability: they survive stop/start
# and restart of a single workspace (lost only on a permanent delete). They are NOT
# under runtime/ (which the opt-in GitHub sync ships to a git branch) -- a fat,
# churny profile would bloat that branch. Override the root for tests / alternate layouts.
_PROFILE_ROOT = Path(
    os.environ.get(
        "BROWSER_PROFILE_ROOT",
        str(Path(os.environ.get("MNGR_HOST_DIR", "/home/user/.mngr")) / "browser-profiles"),
    )
)
# Seconds to wait for one tab's navigation during restore, so a slow SSO redirect
# can't stall the sequential relaunch of the rest of the fleet.
_RESTORE_NAV_TIMEOUT = float(os.environ.get("BROWSER_RESTORE_NAV_TIMEOUT", "20"))
# How often the manager re-checkpoints the manifest (a no-op when nothing changed).
# Topology changes (create/close) checkpoint immediately; this catches tab-URL drift
# so an ungraceful daemon kill loses at most this many seconds of tab changes (the
# profile's cookies/logins persist regardless).
_MANIFEST_CHECKPOINT_SECONDS = float(os.environ.get("BROWSER_CHECKPOINT_SECONDS", "10"))
# Lock files Chromium leaves in a profile; a hard kill (crash/OOM/container stop)
# orphans them and the next launch on that profile would refuse to start. Safe to
# remove because restore is sequential and the prior Chromium for this dir is dead.
_SINGLETON_LOCK_NAMES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _profile_dir(browser_id: str) -> Path:
    """The persistent Chromium ``user_data_dir`` for a browser name.

    The ``browser-use-user-data-dir-`` prefix in the final path component is
    LOAD-BEARING, not cosmetic: browser_use's ``BrowserProfile._copy_profile()``
    (profile.py) treats any other path as a "real" profile to COPY into a throwaway
    temp dir (because the bundled binary is "Google Chrome for Testing", so its
    is_chrome check is True) -- which would silently defeat persistence and recopy
    50-500MB on every launch. A path containing this substring hits its early-return
    and is used in place. Pinned by browser-use==0.13.1 and guarded by an integration
    test; do not rename without updating that test. Only the suffix changed from an
    int to the name string (validated filesystem-safe by names.is_valid_browser_name).
    """
    return _PROFILE_ROOT / f"browser-use-user-data-dir-{browser_id}"


def _clear_stale_singleton(profile_dir: Path) -> None:
    """Remove Chromium's Singleton* lock files left behind by a hard kill, so a
    relaunch on this persistent profile isn't refused. Called only at launch, never
    while a browser is live (one live Chromium per profile dir)."""
    for name in _SINGLETON_LOCK_NAMES:
        try:
            (profile_dir / name).unlink(missing_ok=True)
        except OSError as e:
            logger.debug("could not clear {} in {} ({})", name, profile_dir, e)


def _is_restorable_url(url: str | None) -> bool:
    """Whether a tab URL is worth persisting/reopening (skip blank and internal pages)."""
    return bool(url) and not url.startswith(("about:", "chrome:", "chrome-error:", "devtools:"))


def _action_summary(action: Any) -> str:
    """One-line label for a browser-use action dict (e.g. ``switch: {"tab_id": "230B"}``)."""
    if isinstance(action, dict):
        for key, value in action.items():
            if key == "interacted_element":
                continue
            if value is None or (isinstance(value, dict) and not value):
                return str(key)
            return f"{key}: {json.dumps(value, default=str)}"
    return str(action)[:80]


def _parse_env_file(text: str) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env file (the format claude_auth writes), tolerating quotes."""
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1].replace('\\"', '"')
        if key:
            result[key] = value
    return result


def resolve_anthropic_key() -> str | None:
    """Return a direct Anthropic API key from the process env or ``$MNGR_HOST_DIR/env``.

    Anthropic API only: we deliberately do NOT read ``ANTHROPIC_BASE_URL`` / support
    the Imbue Cloud / litellm proxy path. The fallback re-reads the host env file fresh
    so a key submitted after this service started is still found without a restart.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        host_dir = os.environ.get("MNGR_HOST_DIR")
        if host_dir:
            env_path = Path(host_dir) / "env"
            if env_path.exists():
                api_key = _parse_env_file(env_path.read_text()).get("ANTHROPIC_API_KEY")
    return api_key


def anthropic_key_status() -> tuple[bool, str]:
    """Return ``(available, reason)`` for the optional, key-only ``task``/``extract``
    verbs. Direct control (state/click/input/scroll/...) is keyless and always
    available, so this never gates starting or driving a browser -- only those two
    verbs, which the daemon checks at call time."""
    if resolve_anthropic_key():
        return True, "Anthropic API key available"
    return (
        False,
        "The 'task' and 'extract' verbs need an Anthropic API key (create the workspace "
        "with the 'Anthropic API key' provider; the 'Claude subscription' option has no "
        "usable key). Direct control -- state/click/input/scroll/screenshot/tab -- works "
        "without one.",
    )


def deferred_install_ready() -> tuple[bool, str]:
    """Return ``(ready, reason)`` once Chromium is installed."""
    if os.environ.get("BROWSER_SKIP_INSTALL_CHECK") == "1":
        return True, "ready"  # host/CI testing without an installed Fortress
    if not os.access(_FORTRESS_EXECUTABLE, os.X_OK):
        return False, "Chromium is still installing in this workspace; try again in a minute."
    # Headful needs the Xvfb display present; wait for its env.d install too
    # (headless runs -- tests, bare dev boxes -- don't need it). Like Fortress,
    # gate on the binary itself: env-converge units leave no marker files.
    if not _HEADLESS and shutil.which("Xvfb") is None:
        return False, "The virtual display is still installing in this workspace; try again in a minute."
    return True, "ready"


def _enabled_event() -> asyncio.Event:
    """An asyncio.Event that starts SET -- the resting controller is the human, so
    human input is enabled from construction (not only after :meth:`LiveBrowser.start`)."""
    event = asyncio.Event()
    event.set()
    return event


class BrowserStartupError(Exception):
    """Raised when a Chromium session fails to come up (e.g. no CDP endpoint)."""


class FleetFullError(BrowserStartupError):
    """Raised when the fleet is already at ``_MAX_SESSIONS`` (maps to HTTP 409)."""


class InvalidBrowserNameError(BrowserStartupError):
    """Raised when a user-typed browser name is syntactically invalid (maps to HTTP 400)."""


class DuplicateBrowserNameError(BrowserStartupError):
    """Raised when a user-typed name collides with a live browser (maps to HTTP 409).

    A crashed-but-not-closed browser still holds its name, so a duplicate can mean a
    dead shell is reserving it -- close that one to free the name (see the CLI/SKILL).
    """


class _AcquireWaiter:
    """One agent parked in a browser's FIFO wait-queue (monitor-and-wait).

    ``event`` is set when the waiter is resolved; ``granted`` distinguishes the two
    outcomes -- handed ownership (the prior agent released) vs evicted because a
    human took control (agents never wait on a human-pinned browser).
    """

    def __init__(self, agent_id: str, agent_name: str | None) -> None:
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.event = asyncio.Event()
        self.granted = False


class LiveBrowser(MutableModel):
    """One headless Chromium streamed to the user, optionally driven by a browser-use agent."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    # The random ~2-word english NAME the user/agent sees (e.g. "alex-smith"). The
    # addressing key everywhere (CLI arg, cast WS path, manifest id, profile dir).
    # Stable and never reused: a closed name is gone, so a cached name is the same
    # browser or a 404. There is no default browser -- every name is created on demand.
    browser_id: str
    controller: ControlOwner = "human"
    owner_agent_id: str | None = None
    owner_agent_name: str | None = None
    human_pinned: bool = False

    _playwright: Playwright = PrivateAttr()
    _bu_session: BrowserSession = PrivateAttr()
    _observer: Browser | None = PrivateAttr(default=None)
    _context: BrowserContext | None = PrivateAttr(default=None)
    _active_page: Page | None = PrivateAttr(default=None)
    _active_cdp: CDPSession | None = PrivateAttr(default=None)
    _agent: Agent | None = PrivateAttr(default=None)
    _agent_task: "asyncio.Task[None] | None" = PrivateAttr(default=None)
    # The in-flight direct-control action (a single click/type/navigate), set for the
    # duration of run_action's action. A human take_control cancels it (so the human's
    # XTest never overlaps an agent's still-running CDP action), and the idle-lease sweep
    # skips while it's set (a long action must not be swept out from under itself).
    _direct_action_task: "asyncio.Task[Any] | None" = PrivateAttr(default=None)
    _run_on_event: EventSink | None = PrivateAttr(default=None)
    _input_enabled: asyncio.Event = PrivateAttr(default_factory=_enabled_event)
    # Outbound fan-out queues, one per connected cast WebSocket. The WS lives on a
    # Flask thread (thread-per-connection); the loop pushes JSON frames onto its
    # queue and the Flask thread drains and sends them. queue.Queue is thread-safe
    # for the one-producer (loop) / one-consumer (Flask) handoff. The LIST itself is
    # mutated ONLY on the loop thread (register/unregister are awaited via the
    # bridge), so _broadcast can iterate it without a lock -- the single-loop
    # serialization is the guard. Mirrors system/libs/system_interface's WebSocketBroadcaster.
    _cast_queues: list["queue.Queue[str | None]"] = PrivateAttr(default_factory=list)
    _nav_tracked: set[Page] = PrivateAttr(default_factory=set)
    _active_target_id: str | None = PrivateAttr(default=None)
    # Per-browser X display (Xvfb) + its striped H.264/JPEG encoder. None when headless
    # (tests / bare dev boxes): the human view + display input are simply unavailable
    # there, while the agent's CDP control keeps working. Set in start(), torn down in
    # close().
    _display: Display | None = PrivateAttr(default=None)
    _capture: Capture | None = PrivateAttr(default=None)
    # Per-browser audio: its own PulseAudio null sink (Chromium plays into it via
    # PULSE_SINK, so sound is isolated per browser) captured on demand to PCM. Best-effort:
    # None when PulseAudio isn't available -- the browser just has no sound.
    _audio: "AudioCapture | None" = PrivateAttr(default=None)
    _audio_sink_module: str | None = PrivateAttr(default=None)
    # Browser-level CDP session (distinct from a page target's) for window bounds --
    # resize-to-pane sets Browser.setWindowBounds and measures the chrome height.
    _browser_cdp: CDPSession | None = PrivateAttr(default=None)
    _window_id: int | None = PrivateAttr(default=None)
    # Clipboard copy-out watcher: the task, the hash of the value WE last put on the
    # clipboard (so a paste/copy we made isn't echoed back to the user), and the hash
    # of the last value we pushed out (so we push each remote copy once).
    _clip_watch_task: "asyncio.Task[None] | None" = PrivateAttr(default=None)
    _own_clip_hash: int | None = PrivateAttr(default=None)
    _seen_clip_hash: int | None = PrivateAttr(default=None)
    _keepalive_task: "asyncio.Task[None] | None" = PrivateAttr(default=None)
    # The in-flight serialized launch task (set by the manager's _spawn_launch). close()
    # awaits it via the manager so a teardown can't race a suspended start() -- the launch
    # finishes/aborts first and observes _closed. None once create's launch isn't pending.
    _launch_task: "asyncio.Task[None] | None" = PrivateAttr(default=None)
    _closed: bool = PrivateAttr(default=False)
    # Serializes screencast/active-tab changes (slow CDP work).
    _lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    # Serializes ALL ownership changes -- the single mutual-exclusion primitive.
    _control_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    _wait_queue: list[_AcquireWaiter] = PrivateAttr(default_factory=list)
    # Direct-control: browser-use's own action executor (lazily bound to _bu_session),
    # the last `state`'s numbered elements (so `click <index>` resolves a node), and
    # the sticky-lease activity timestamp the idle-TTL sweep checks.
    _action_handler: ActionHandler | None = PrivateAttr(default=None)
    _selector_map: dict[int, Any] = PrivateAttr(default_factory=dict)
    _lease_touched_at: float = PrivateAttr(default=0.0)
    _screenshot_seq: int = PrivateAttr(default=0)
    # Live render size (the browser WINDOW / capture region), grown to the human's pane
    # by _apply_resize between the floor (_RENDER_MIN_*) and cap (_RENDER_MAX_*). Frozen
    # while an agent drives so its `state` element indices don't shift mid-task.
    _render_w: int = PrivateAttr(default=_RENDER_DEFAULT_WIDTH)
    _render_h: int = PrivateAttr(default=_RENDER_DEFAULT_HEIGHT)
    # The render size the current agent started with -- compared on resume to tell it
    # if the human resized (reflowing the page) while they held control (see _wake_agent).
    _agent_render_w: int = PrivateAttr(default=_RENDER_DEFAULT_WIDTH)
    _agent_render_h: int = PrivateAttr(default=_RENDER_DEFAULT_HEIGHT)
    # Direct-control resume queue: agents whose command was rejected (a human or
    # another agent held the browser). They ended their turns; when the browser
    # frees they are handed it FIFO and messaged to resume (see _wake_agent). This
    # is separate from _wait_queue (the connection-bound blocking waiters used by
    # `task`/`hold`). ``(agent_id, agent_name)`` per entry, deduped by id.
    _resume_queue: list[tuple[str, str | None]] = PrivateAttr(default_factory=list)
    # When a resume-queue agent was handed the browser but hasn't sent a command
    # yet (the claim window); 0.0 once it claims (or when no grant is pending).
    _granted_at: float = PrivateAttr(default=0.0)
    # Strong refs to in-flight fire-and-forget tasks (the _wake_agent subprocess, the
    # crash announcement). asyncio keeps only weak references to bare create_task()
    # results, so without this they could be garbage-collected before they run.
    _bg_tasks: set[Any] = PrivateAttr(default_factory=set)
    # The single explicit lifecycle field (see ``Lifecycle``). A browser is registered
    # in ``init`` (Chromium not yet up), flips to ``running`` once Chromium is up and the
    # screencast is attached, and to ``crashed`` if Chromium dies unexpectedly (OS/OOM
    # kill, segfault) -- detected via the Playwright observer's `disconnected` event, or
    # lazily when an action finds the connection gone. A crashed browser reports
    # "crashed" to agents and the viewer rather than silently freezing; its name is never
    # reused (a new browser gets a new random name), so the dead one stays clearly
    # labeled until it is closed. All transitions stay on the single loop thread, so this
    # plain field needs no lock (cooperative single-thread atomicity).
    _lifecycle: Lifecycle = PrivateAttr(default="init")
    # Set by the manager: a no-arg hook that checkpoints the fleet manifest. Fired on
    # crash so a browser that died is dropped from the manifest promptly (not only on
    # the next ~10s checkpoint tick), so an ungraceful kill right after a crash doesn't
    # restore the dead browser as healthy next boot.
    _crash_save_hook: "Callable[[], None] | None" = PrivateAttr(default=None)

    @property
    def _crashed(self) -> bool:
        """Whether Chromium died unexpectedly. Backed by the single ``_lifecycle`` field
        (``crashed`` is terminal). A property -- not a separate flag -- so there is one
        source of truth; the setter exists so the crash-detection paths (and tests) can
        keep writing ``self._crashed = True`` while the real state lives in
        ``_lifecycle``."""
        return self._lifecycle == "crashed"

    @_crashed.setter
    def _crashed(self, value: bool) -> None:
        if value:
            self._lifecycle = "crashed"
        elif self._lifecycle == "crashed":
            self._lifecycle = "init"

    @property
    def _is_running(self) -> bool:
        """Chromium is up and the screencast attached -- the only state in which the
        browser can be driven and the viewer shows the live page."""
        return self._lifecycle == "running"

    def _build_bu_session(self, profile_dir: Path, chromium_path: str, *, chromium_sandbox: bool) -> BrowserSession:
        """Construct (don't start) the browser-use session for this browser's persistent
        profile. ``chromium_sandbox`` is False when Chromium's in-process sandbox must be
        disabled (see _NO_SANDBOX / the start() fallback); browser-use then injects
        ``--no-sandbox`` itself."""
        # Headful: no_viewport=True so browser-use does NOT pin a per-tab device-metrics
        # override -- the page viewport tracks the real window, which is what lets a
        # window resize actually reflow the page for the display capture (browser-use
        # still reads the real viewport via getLayoutMetrics for element indexing).
        # Headless (tests) keeps a fixed viewport, since browser-use asserts headless
        # runs have one. We deliberately do NOT set storage_state (it would overwrite
        # the live persistent profile, whose dir name is load-bearing -- see _profile_dir).
        kwargs: dict[str, Any] = dict(
            headless=_HEADLESS,
            executable_path=chromium_path,
            user_data_dir=str(profile_dir),
            # --remote-allow-origins=*: newer Chromium (150+) rejects a CDP WebSocket
            # whose Origin header it doesn't allow, which hangs our observer's
            # connect_over_cdp. Allowing any origin is the standard fix for driving a
            # local Chromium over CDP (loopback-only debugging port; not web-reachable).
            # --disable-dev-shm-usage: use /tmp not /dev/shm (some containers cap /dev/shm).
            # --remote-allow-origins=*: newer Chromium rejects a CDP WebSocket whose Origin
            # it doesn't allow (see below). The rest trim Chromium's cold-start: skip the
            # first-run/onboarding/search-engine dialogs and background component/sync work
            # that otherwise add seconds to a fresh-profile launch (every new browser is a
            # fresh profile). None touch the page-facing fingerprint, so stealth is intact.
            args=[
                "--disable-dev-shm-usage",
                "--remote-allow-origins=*",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-search-engine-choice-screen",
                "--disable-component-update",
                "--disable-sync",
                "--disable-features=Translate,MediaRouter",
                # Keep Chromium ALIVE when the human closes the last tab. Normally the last
                # window closing exits the process, which our crash detector then (correctly,
                # for that signal) reports as a crash -- so closing all tabs looked like a
                # crash. With this, the process stays up window-less; _on_page_closed then
                # reliably opens a fresh home tab (no race against Chromium's shutdown).
                "--keep-alive-for-test",
            ],
            # Strip browser-use's default --disable-gpu-sandbox: it's redundant with
            # --no-sandbox and triggers Chromium's yellow "unsupported command-line flag"
            # infobar -- which, now that we stream the WHOLE browser window, the user sees.
            ignore_default_args=["--disable-gpu-sandbox"],
            chromium_sandbox=chromium_sandbox,
            keep_alive=True,
            window_size={"width": _RENDER_DEFAULT_WIDTH, "height": _RENDER_DEFAULT_HEIGHT},
            device_scale_factor=1,
        )
        if _HEADLESS:
            kwargs["viewport"] = {"width": _RENDER_DEFAULT_WIDTH, "height": _RENDER_DEFAULT_HEIGHT}
        else:
            kwargs["no_viewport"] = True
        return BrowserSession(**kwargs)

    async def _start_bu_session(self, profile_dir: Path, chromium_path: str) -> BrowserSession:
        """Launch the browser-use session. The Chromium sandbox is disabled up front when
        we run as root or BROWSER_NO_SANDBOX is set (see _should_disable_sandbox) -- so on
        the bare-VM Lima case we never make the doomed sandboxed attempt that browser-use
        turns into a 30s hang. As a backstop, if a *sandboxed* launch still fails we retry
        once with the sandbox off (the only thing the retry changes), covering any non-root
        runtime that also can't sandbox."""
        disable_sandbox = _should_disable_sandbox()
        session = self._build_bu_session(profile_dir, chromium_path, chromium_sandbox=not disable_sandbox)
        try:
            await session.start()
        except (BrowserStartupError, *_BROWSER_ERRORS) as e:
            if disable_sandbox:  # sandbox was already off -> the failure is something else
                raise
            logger.warning(
                "browser {} failed to launch ({}); retrying without the Chromium sandbox", self.browser_id, e
            )
            _clear_stale_singleton(profile_dir)
            session = self._build_bu_session(profile_dir, chromium_path, chromium_sandbox=False)
            await session.start()
        return session

    async def start(
        self, playwright: Playwright, restore_tabs: list[str] | None = None, active_tab: int = 0
    ) -> None:
        """Launch the headless Chromium (browser-use) and attach the Playwright observer.

        Uses a persistent ``user_data_dir`` per browser name so cookies/logins/history
        survive a restart (Chromium's own persistence; we serialize none of it). When
        ``restore_tabs`` is given (a list of URLs from the manifest), reopen those tabs
        in order instead of the single default home page (and re-focus ``active_tab``);
        the persistent profile means they come back logged in.
        """
        self._playwright = playwright
        self._input_enabled.set()
        # Fixed Fortress path, not playwright.chromium.executable_path -- the
        # fleet's engine is Fortress, not Playwright's own managed Chromium
        # (which vanilla Playwright calls elsewhere in this image still use).
        chromium_path = _FORTRESS_EXECUTABLE
        profile_dir = _profile_dir(self.browser_id)
        profile_dir.mkdir(parents=True, exist_ok=True)
        _clear_stale_singleton(profile_dir)  # a prior hard kill may have orphaned a lock
        # Bring up this browser's own X display (headful) and point Chromium at it by
        # mutating DISPLAY around the launch: browser-use spawns Chromium as a subprocess
        # that inherits os.environ and takes no env override. Saved + restored so a
        # concurrent capture-start's DISPLAY change can't leak in (the loop is single-
        # threaded and browser launches are serialized by the manager's _startup_lock,
        # so only this launch's DISPLAY is live across its awaits).
        if not _HEADLESS:
            try:
                self._display = Display()
                await self._display.start()
            except (DisplayError, OSError) as e:
                raise BrowserStartupError(f"could not start the browser's X display: {e}") from e
        prior_display = os.environ.get("DISPLAY")
        prior_sink = os.environ.get("PULSE_SINK")
        if self._display is not None:
            os.environ["DISPLAY"] = self._display.name
            # Give this browser its own PulseAudio sink and point Chromium at it (PULSE_SINK,
            # inherited like DISPLAY), so its sound is captured in isolation. Best-effort:
            # if PulseAudio isn't up, no sink -> no PULSE_SINK -> the browser just has no
            # audio, and the launch proceeds normally.
            sink_name = f"mind_{self._display.num}"
            self._audio_sink_module = create_null_sink(sink_name)
            if self._audio_sink_module is not None:
                os.environ["PULSE_SINK"] = sink_name
                self._audio = AudioCapture(["-f", "pulse", "-i", f"{sink_name}.monitor"])
        try:
            self._bu_session = await self._start_bu_session(profile_dir, chromium_path)
        finally:
            if self._display is not None:
                if prior_display is None:
                    os.environ.pop("DISPLAY", None)
                else:
                    os.environ["DISPLAY"] = prior_display
            if self._audio_sink_module is not None:
                if prior_sink is None:
                    os.environ.pop("PULSE_SINK", None)
                else:
                    os.environ["PULSE_SINK"] = prior_sink
        # The Chromium tree just spawned (and its processes self-write their
        # oom_score_adj moments later): have the OOM sweep re-band it.
        notify_chromium_processes_expected()
        # close() may have run while we were suspended in _start_bu_session (it holds no
        # lock and pops the browser before this resumes). If so, abort -- and kill the
        # Chromium we just brought up, so we don't leak a second handle behind a browser
        # that's already been torn down / removed.
        if await self._abort_start_if_torn_down():
            return
        cdp_url = self._bu_session.cdp_url
        if not cdp_url:
            raise BrowserStartupError("browser-use BrowserSession did not expose a cdp_url after start")
        observer = await playwright.chromium.connect_over_cdp(cdp_url)
        self._observer = observer
        # Detect an unexpected Chromium death (OS/OOM kill, segfault): the observer's
        # CDP connection drops and Playwright fires `disconnected`. Our own close()
        # also fires it, so the handler ignores the case where _closed is already set.
        observer.on("disconnected", self._on_disconnected)
        self._context = observer.contexts[0] if observer.contexts else await observer.new_context()
        self._context.on("page", self._on_new_page)
        pages = self._context.pages
        page = pages[0] if pages else await self._context.new_page()
        self._track_nav(page)
        await self._set_active_page(page)
        # Re-check ONE more time right before the terminal flip: a close() (or a crash
        # detected via the observer's disconnected event) may have landed during any of
        # the awaits above (connect_over_cdp / _set_active_page). Without this we'd flip a
        # torn-down / removed browser to "running". The observer is already attached here,
        # so close()'s own teardown covers the Chromium kill -- but if we got here via a
        # launch that close() didn't serialize against, kill it ourselves to be safe.
        if await self._abort_start_if_torn_down():
            return
        # Headful: attach the display encoder (started on demand by the first /stream
        # subscriber), measure the window so resize works, and start the clipboard watcher.
        if self._display is not None:
            await self._setup_display_capture(observer)
            # Apply the current render size to the real window NOW that CDP + the window id
            # exist. A pane-size resize can arrive DURING startup (the viewer reports its
            # size as soon as its cast socket opens, well before this point); that early
            # resize updated _render_w/h but _resize_window() no-op'd because the browser
            # CDP wasn't attached yet. Without this, the window would keep its default
            # launch size while _render_w/h says the pane size -- the "doesn't fit the pane
            # until I manually resize" bug -- and the capture region (== _render_w/h) would
            # mismatch the window. Aligning here makes first load fit the pane.
            await self._resize_window()
            self._clip_watch_task = asyncio.create_task(self._clipboard_out_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        # _setup_display_capture / _resize_window above await real CDP round trips, during
        # which Chromium can be OOM-killed (it's most vulnerable early, before its oom_score
        # is re-banded) or close() can land -- either fires _on_disconnected -> _crashed, or
        # sets _closed. Re-check BEFORE flipping to running, or we'd clobber that terminal
        # state back to "running" and report a dead browser as healthy (its own crash cleanup
        # already ran; we just must not overwrite it).
        if self._crashed or self._closed:
            return
        # Flip init -> running NOW -- Chromium + the encoder are ready -- so the viewer
        # starts streaming AT ONCE and watches the home page load in the live view, rather
        # than waiting behind the "Starting browser…" overlay through a slow first nav.
        self._lifecycle = "running"
        self._broadcast(self._control_message())
        logger.info("LiveBrowser {} started (cdp_url={}, display={})",
                    self.browser_id, cdp_url, self._display.name if self._display else "headless")
        # Navigate to the home page (or restore saved tabs) AFTER going running, so the
        # user sees the browser + loading progress instead of a blank wait. A close during
        # this nav is handled by close()/the _BROWSER_ERRORS guards in _open_initial_tabs.
        await self._open_initial_tabs(page, restore_tabs, active_tab)

    async def _setup_display_capture(self, observer: Browser) -> None:
        """Wire up the per-browser display encoder: a browser-level CDP session (for
        window bounds), a one-off chrome-height measurement so the capture region crops
        the tab strip + toolbar away, and the pixelflux :class:`Capture` itself. Best-
        effort: a failure here leaves the browser usable for the agent (CDP) but without
        the human view, rather than failing the whole launch."""
        assert self._display is not None
        try:
            self._browser_cdp = await observer.new_browser_cdp_session()
            await self._measure_window()
            # Grant the JS Clipboard API to every page. Without this the browser denies
            # navigator.clipboard.writeText/read with "permission denied", so a website's
            # own "Copy" button silently fails (its result never reaches the X clipboard the
            # human's live-view copy-out reads). The human drives this browser, so it's the
            # same trust as their own browser granting clipboard access on a gesture.
            await self._browser_cdp.send("Browser.grantPermissions", {
                "permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"],
            })
        except _BROWSER_ERRORS as e:
            logger.warning("browser {} display-capture setup degraded ({})", self.browser_id, e)
        # Give the Chromium window explicit X input focus ONCE, now it's mapped. On a bare
        # Xvfb (no window manager) focus stays PointerRoot, so document.hasFocus() is false and
        # the JS Clipboard API rejects (website "Copy" buttons fail). Focus persists with no WM
        # to steal it, so this one call suffices -- no need to re-assert on every click.
        self._display.focus_window()
        self._capture = Capture(self._display.name, self._capture_region)

    def _capture_region(self) -> tuple[int, int, int, int]:
        """Current pixelflux capture rect on the display: the WHOLE Chromium window --
        native tab strip, toolbar, and URL bar included -- at the live render size. The
        window sits at (0,0) (browser-use injects --window-position=0,0), so input coords
        map straight through (no crop offset)."""
        return (0, 0, self._render_w, self._render_h)

    async def _measure_window(self) -> None:
        """Record the browser window id (for resize). The live view streams the whole
        Chromium window -- the user drives its native chrome (tabs, new-tab, URL bar) via
        XTest -- so there is nothing to crop."""
        if self._browser_cdp is None or self._active_target_id is None:
            return
        try:
            win = await self._browser_cdp.send("Browser.getWindowForTarget", {"targetId": self._active_target_id})
            self._window_id = win["windowId"]
        except _BROWSER_ERRORS as e:
            logger.debug("getWindowForTarget ignored ({})", e)

    async def _abort_start_if_torn_down(self) -> bool:
        """If close() or a crash landed while ``start`` was suspended at an await, abort the
        launch: kill the Chromium we already brought up (so a close()-during-launch can't
        leak a second handle) and report True so ``start`` returns without flipping to
        ``running``. Returns False (and does nothing) on the normal path. Idempotent and
        cheap; called at start()'s yield points after the bu_session exists."""
        if not (self._closed or self._crashed):
            return False
        bu_session = getattr(self, "_bu_session", None)
        if bu_session is not None:
            try:
                await bu_session.kill()
            except _BROWSER_ERRORS as e:
                logger.debug("aborted-launch kill ignored ({})", e)
        return True

    async def _open_initial_tabs(
        self, first_page: Page, restore_tabs: list[str] | None, active_tab: int = 0
    ) -> None:
        """Navigate the initial page(s): the saved tabs on restore, else the home page,
        then re-focus the tab that was active before the restart.

        Each navigation is bounded by ``_RESTORE_NAV_TIMEOUT`` so one slow/hung URL
        can't stall startup, and failures are swallowed (a tab that won't load just
        comes up blank -- the profile's cookies are already attached either way)."""
        # start() assigns _context immediately before calling this, so it is always set
        # here; assert it for the type checker and to explode loudly if that ever changes.
        context = self._context
        assert context is not None, "_open_initial_tabs called before the browser context was attached"
        urls = [u for u in (restore_tabs or []) if _is_restorable_url(u)] or [_HOME_URL]

        async def _go(page: Page, url: str) -> None:
            try:
                await asyncio.wait_for(page.goto(url), timeout=_RESTORE_NAV_TIMEOUT)
            except (TimeoutError, *_BROWSER_ERRORS) as e:
                logger.debug("restore nav to {} ignored ({})", url, e)

        pages = [first_page]
        await _go(first_page, urls[0])
        for url in urls[1:]:
            try:
                page = await context.new_page()
            except _BROWSER_ERRORS as e:
                logger.debug("restore new-tab for {} ignored ({})", url, e)
                continue
            self._track_nav(page)
            pages.append(page)
            await _go(page, url)
        # Re-focus the tab that was active before the restart (each new_page above
        # made itself active, so without this the LAST tab would be foregrounded).
        if 0 <= active_tab < len(pages) and pages[active_tab] is not self._active_page:
            await self._set_active_page(pages[active_tab])

    # --- active tab ----------------------------------------------------------

    async def _set_active_page(self, page: Page) -> None:
        """Make ``page`` the input/agent target and refresh the tab list, taking ``_lock``.

        The live *view* is a capture of the whole display, which always shows whatever
        tab Chromium has in front -- so switching tabs (via ``bring_to_front``) is all
        it takes; we just re-key the video so the new tab shows immediately and keep a
        page-level CDP session for target/window queries. Serialized by ``_lock`` so
        overlapping calls (rapid navigations each firing framenavigated) can't interleave
        at the detach/attach boundary and leak a CDP session.

        Callers that ALREADY hold ``_lock`` (the agent's ``run_action`` path, via
        ``act_tab``) must use :meth:`_set_active_page_locked` instead -- re-taking the
        non-reentrant ``_lock`` here would deadlock the browser.
        """
        async with self._lock:
            await self._set_active_page_locked(page)

    async def _set_active_page_locked(self, page: Page) -> None:
        """The body of :meth:`_set_active_page`; the caller must hold ``_lock``."""
        if self._context is None:
            return  # torn down -- close() raced a queued nav re-attach
        if self._active_cdp is not None:
            await self._detach_active_cdp()
        # Re-check after the await above: close() doesn't take _lock, so it can
        # null self._context while _detach_active_cdp() yields. Without this guard
        # new_cdp_session(page) would dereference None and the orphaned task's
        # AttributeError surfaces as "Task exception was never retrieved".
        if self._context is None:
            return  # torn down mid-teardown -- nothing to (re)attach to
        self._active_page = page
        try:
            cdp = await self._context.new_cdp_session(page)
            self._active_cdp = cdp
            try:
                info = await cdp.send("Target.getTargetInfo")
                self._active_target_id = info["targetInfo"]["targetId"]
            except _BROWSER_ERRORS:
                self._active_target_id = None
        except _BROWSER_ERRORS as e:
            logger.debug("active-page cdp attach ignored ({})", e)
            return
        if self._capture is not None:
            self._capture.request_keyframe()  # new tab in front -> push a clean frame now
        await self._broadcast_tabs()

    async def _detach_active_cdp(self) -> None:
        cdp = self._active_cdp
        self._active_cdp = None
        if cdp is None:
            return
        try:
            await cdp.detach()
        except _BROWSER_ERRORS as e:
            logger.debug("cdp detach ignored ({})", e)

    def _on_new_page(self, page: Page) -> None:
        """A new tab appeared (human or agent opened it): follow it."""
        # A new page usually means a new renderer process about to self-write
        # its oom_score_adj; have the OOM sweep re-band it.
        notify_chromium_processes_expected()
        asyncio.create_task(self._follow_new_page(page))

    async def _follow_new_page(self, page: Page) -> None:
        self._track_nav(page)
        try:
            await page.wait_for_load_state("domcontentloaded")
            await self._set_active_page(page)
        except _BROWSER_ERRORS as e:
            logger.debug("follow new page ignored ({})", e)

    def _track_nav(self, page: Page) -> None:
        """Re-point the video + refresh tabs on navigation, and handle tab close.

        A cross-origin navigation swaps the CDP target, so re-running _set_active_page
        rebinds the page-level CDP session and re-broadcasts the tab list. The close hook
        also guards the LAST tab: the human closes tabs on the NATIVE chrome, and closing
        the final one would exit Chromium (the browser would look "crashed") -- so we
        best-effort reopen a home tab to keep it alive.
        """
        if page in self._nav_tracked:
            return
        self._nav_tracked.add(page)
        page.on("framenavigated", lambda frame, captured=page: self._on_page_nav(frame, captured))
        page.on("close", lambda _p: asyncio.create_task(self._on_page_closed()))

    async def _on_page_closed(self) -> None:
        await self._broadcast_tabs()
        context = self._context
        if context is None or self._closed or self._crashed or context.pages:
            return
        # Last tab gone (a human closed it on the native chrome): open a fresh home tab so
        # the browser survives instead of exiting. Best-effort -- it races Chromium's
        # shutdown; if we lose, the disconnect path reports a genuine close/crash.
        try:
            page = await context.new_page()
            await page.goto(_HOME_URL)
            await self._set_active_page(page)
        except _BROWSER_ERRORS as e:
            logger.debug("last-tab reopen lost the race for browser {} ({})", self.browser_id, e)

    def _on_page_nav(self, frame: Any, page: Page) -> None:
        # Any navigation (any frame, human- or agent-driven) can swap in a new
        # renderer process (site isolation); have the OOM sweep re-band it.
        notify_chromium_processes_expected()
        if page is self._active_page and frame == page.main_frame:
            asyncio.create_task(self._set_active_page(page))

    # --- stream (video) subscribers -------------------------------------------
    # Async so they run ON the loop (reached via the runner's bridge): starting the
    # encoder briefly mutates os.environ["DISPLAY"], which must happen on the single
    # loop thread where browser launches also serialize -- never on a Flask thread.

    async def add_stream_subscriber(self, want_h264: bool) -> "queue.Queue[bytes | None] | None":
        """Register a ``/stream`` video socket, starting the display encoder on demand.
        Returns its outbound byte queue, or None when there's no encoder (headless, the
        browser isn't running, or it's being torn down) -- the handler closes the socket.
        The ``_closed`` guard matters: a subscribe can race close() (the /stream handshake
        waits before subscribing), and starting a capture against a just-freed display
        whose number may be reused would stream the WRONG browser."""
        if self._capture is None or not self._is_running or self._closed:
            return None
        return self._capture.add_subscriber(want_h264)

    async def remove_stream_subscriber(self, client_queue: "queue.Queue[bytes | None]") -> None:
        if self._capture is not None:
            self._capture.remove_subscriber(client_queue)

    async def add_audio_subscriber(self) -> "queue.Queue[bytes | None] | None":
        """Register an ``/audio`` PCM socket, starting the sink capture on demand. Returns
        its outbound queue, or None when there's no audio (no PulseAudio sink, the browser
        isn't running, or it's tearing down) -- the handler closes the socket, viewer retries."""
        if self._audio is None or not self._is_running or self._closed:
            return None
        return self._audio.add_subscriber()

    async def remove_audio_subscriber(self, client_queue: "queue.Queue[bytes | None]") -> None:
        if self._audio is not None:
            self._audio.remove_subscriber(client_queue)

    async def _broadcast_tabs(self) -> None:
        # Stays async: it awaits _tab_list() (a CDP round-trip). The fan-out itself
        # (_broadcast) is now synchronous.
        self._broadcast({"type": "tabs", "tabs": await self._tab_list()})

    async def _tab_list(self) -> list[dict[str, Any]]:
        if self._context is None:
            return []
        tabs = []
        for index, page in enumerate(self._context.pages):
            tabs.append(
                {
                    "index": index,
                    "title": (await _safe_title(page)),
                    "url": page.url,
                    "active": page is self._active_page,
                }
            )
        return tabs

    def tab_urls(self) -> tuple[list[str], int]:
        """The restorable tab URLs + the active tab's index within them, for the
        manifest. ``page.url`` is a cached property (no CDP round-trip), unlike the
        title fetch in ``_tab_list`` -- so the periodic checkpoint stays cheap."""
        if self._context is None:
            return [], 0
        urls: list[str] = []
        active = 0
        for page in self._context.pages:
            if _is_restorable_url(page.url):
                if page is self._active_page:
                    active = len(urls)
                urls.append(page.url)
        return urls, active

    async def _follow_agent_focus(self) -> None:
        """Re-point the screencast to the tab the agent just switched to.

        New tabs and navigations are already followed (``_on_new_page`` /
        ``framenavigated``); this covers the agent activating an already-open
        background tab (``switch_tab``), which fires neither. We match
        browser-use's focused CDP target to one of the observer's pages by URL;
        same-URL tabs are an acceptable ambiguity.
        """
        focus_id = getattr(self._bu_session, "agent_focus_target_id", None)
        cdp, context = self._active_cdp, self._context
        if not focus_id or focus_id == self._active_target_id or cdp is None or context is None:
            return
        try:
            targets = (await cdp.send("Target.getTargets")).get("targetInfos", [])
        except _BROWSER_ERRORS as e:
            logger.debug("getTargets for focus-follow ignored ({})", e)
            return
        focus_url = next(
            (t["url"] for t in targets if t.get("targetId") == focus_id and t.get("type") == "page"),
            None,
        )
        if focus_url is None:
            return
        for page in context.pages:
            if page.url == focus_url and page is not self._active_page:
                await self._set_active_page(page)
                return

    async def _keepalive_loop(self) -> None:
        """Ping cast sockets periodically so a static page (no screencast frames)
        doesn't let the WS proxy time out the idle stream; also sweep idle leases and
        refresh the viewer's idle-countdown / queue display while an agent holds."""
        while not self._closed:
            await asyncio.sleep(_KEEPALIVE_SECONDS)
            self._broadcast({"type": "ping"})
            if not self._is_running:
                continue  # init (no ownership yet) or crashed (dead): no sweeps/handoffs
            changed = await self._sweep_unclaimed_grant() or await self._sweep_idle_lease()
            if not changed and self.controller == "agent":
                self._broadcast(self._control_message())

    async def _sweep_idle_lease(self) -> bool:
        """Release a direct-control lease whose owner has gone quiet (dead/wandered-off
        agent). A running ``task`` (``_agent_task`` set) is connection-bound and exempt;
        the CAS keeps this from clobbering a freshly-handed-off lease. Returns True if it
        released one.

        Snapshot the control fields (controller / _agent_task / owner_agent_id /
        _lease_touched_at) under ``_control_lock`` so the idle check and the expect-tuple
        it builds are taken from one consistent view -- otherwise a concurrent ownership
        change between the reads could yield a torn expect-tuple. The CAS in
        ``_transition`` then re-validates against the live state before mutating.
        """
        async with self._control_lock:
            controller = self.controller
            # A running task OR an in-flight direct action means the agent is actively
            # here -- never sweep it out from under a long command (a slow navigate can
            # outlive the idle TTL).
            agent_running = self._agent_task is not None or self._direct_action_task is not None
            owner_agent_id = self.owner_agent_id
            lease_touched_at = self._lease_touched_at
        if controller == "agent" and not agent_running and time.monotonic() - lease_touched_at > _LEASE_IDLE_TTL:
            return await self._transition(to="human", expect=("agent", owner_agent_id, False))
        return False

    async def _sweep_unclaimed_grant(self) -> bool:
        """A resume-queue agent was handed the browser and messaged to resume, but
        hasn't sent a command within ``_CLAIM_WINDOW`` (it was interrupted/killed, or
        never woke). Revoke the grant so the browser passes to the next waiter instead
        of sitting idle for the full idle-TTL on a no-show. ``_granted_at`` is set only
        for a pending grant and cleared the instant the agent sends its first command
        (``run_action``)."""
        async with self._control_lock:
            if (
                self.controller == "agent"
                and self._granted_at
                and self._agent_task is None
                and self._lease_touched_at < self._granted_at
                and time.monotonic() - self._granted_at > _CLAIM_WINDOW
            ):
                self._granted_at = 0.0
                await self._write_control_locked("human", None, None, pinned=False)
                await self._settle_queue_locked()
                return True
        return False

    def _human_pin_active(self) -> bool:
        """A human pin blocks agents until the human explicitly hands back
        (:meth:`return_to_agents`). Taking control is sticky on purpose -- a human can
        walk away mid-CAPTCHA/login and the browser is never yanked back. A *resting*
        human (controller=human, not pinned) is free: an agent takes it via
        :meth:`acquire`."""
        return self.controller == "human" and self.human_pinned

    # --- input ----------------------------------------------------------------

    async def handle_cast_message(self, message: dict[str, Any]) -> None:
        """Handle a message from a cast socket: human mouse/key input, or a resize.

        The whole browser is streamed, so the human drives tabs / navigation / the URL bar
        on the NATIVE chrome via mouse+key -- the viewer sends no tab/nav messages. Input is
        gated on ``_input_enabled`` (set only while the human controls); the check and the
        XTest injection happen together under ``_control_lock`` so an agent acquiring the
        browser mid-dispatch can't let a stale human input land after the handoff.
        """
        kind = message.get("type")
        if kind == "resize":
            # Same gate as input: _input_enabled is set iff the human (or an idle-free
            # browser) owns it, so resizes are dropped while an agent drives -- that's
            # the "aspect locked during agent control" freeze, for free.
            async with self._control_lock:
                if not self._input_enabled.is_set():
                    logger.info("browser {} resize ignored: input not enabled (an agent controls it)", self.browser_id)
                    return
                await self._apply_resize(message)
        elif kind in ("mouse", "key"):
            async with self._control_lock:
                if not self._input_enabled.is_set():
                    return
                if kind == "mouse":
                    self._inject_mouse(message.get("event", {}))
                else:
                    self._inject_key(message.get("event", {}))
        elif kind == "request_keyframe":
            # The viewer asks for a fresh keyframe when it's connected but hasn't painted a
            # frame yet: it can subscribe just as the encoder starts (the initial IDR races
            # it) and, on a STATIC page, no damage ever triggers another -- so the view would
            # sit blank until the user causes a repaint. It re-asks until a frame lands.
            # Cheap, idempotent, and NOT input-gated (a watcher should see the live page too).
            if self._capture is not None:
                self._capture.request_keyframe()

    def _inject_mouse(self, event: dict[str, Any]) -> None:
        """Inject a human mouse event at the DISPLAY level (XTest), so native context
        menus / ``<select>`` dropdowns / drag all work -- none reachable by CDP's
        page-scoped input. Coords are frame (capture-region) coords; the Display adds
        the chrome-crop offset. No-op when headless (no display)."""
        if self._display is None:
            return
        etype = event.get("type")
        x, y = int(event.get("x", 0)), int(event.get("y", 0))
        if etype == "mouseMoved":
            self._display.move(x, y)
        elif etype in ("mousePressed", "mouseReleased"):
            button = _MOUSE_BUTTONS.get(str(event.get("button", "left")), 1)
            self._display.button(button, etype == "mousePressed", x, y)
        elif etype == "mouseWheel":
            self._display.scroll(float(event.get("deltaX", 0)), float(event.get("deltaY", 0)))

    def _inject_key(self, event: dict[str, Any]) -> None:
        """Inject a human key event via XTest (physical ``code`` -> keysym -> keycode).
        Modifier keys arrive as their own events, so combos replay naturally."""
        if self._display is None:
            return
        self._display.key(str(event.get("code", "")), str(event.get("key", "")), event.get("type") == "keyDown")

    async def _apply_resize(self, message: dict[str, Any]) -> None:
        """Human/idle resized their pane: resize the browser WINDOW to fill it (clamped)
        and move the capture region. Reached only while input is enabled (human owns it),
        so an agent's cached ``state`` indices never shift mid-task. Framebuffer-bounded:
        the window is render height + cropped chrome, kept within the fixed Xvfb screen."""
        try:
            raw_w, raw_h = int(message.get("width", 0)), int(message.get("height", 0))
        except (TypeError, ValueError):
            return  # malformed resize (null / non-numeric) -- ignore, don't kill the socket
        if raw_w <= 0 or raw_h <= 0:
            return  # a hidden pane reports 0x0; don't shove the window down to the floor
        w = max(_RENDER_MIN_WIDTH, min(min(_RENDER_MAX_WIDTH, SCREEN_W), raw_w))
        h = max(_RENDER_MIN_HEIGHT, min(min(_RENDER_MAX_HEIGHT, SCREEN_H), raw_h))
        if (w, h) == (self._render_w, self._render_h):
            return
        logger.info("browser {} resize {}x{} -> {}x{} (was {}x{})",
                    self.browser_id, raw_w, raw_h, w, h, self._render_w, self._render_h)
        self._render_w, self._render_h = w, h
        await self._resize_window()
        # Tell every viewer the new resolution so it resizes its canvas + click mapping;
        # otherwise the canvas stays the old size and clicks land off by the size ratio.
        self._broadcast(self._control_message())

    async def _resize_window(self) -> None:
        """Set the Chromium window to render-size + chrome and re-point the capture at
        the new region. No-op when headless (no window/capture)."""
        if self._browser_cdp is not None and self._window_id is not None:
            try:
                await self._browser_cdp.send("Browser.setWindowBounds", {
                    "windowId": self._window_id,
                    "bounds": {"left": 0, "top": 0, "width": self._render_w, "height": self._render_h},
                })
            except _BROWSER_ERRORS as e:
                logger.debug("setWindowBounds ignored ({})", e)
        if self._capture is not None:
            self._capture.update_region()

    async def _handle_tab_control(self, message: dict[str, Any]) -> None:
        """Agent tab control (from ``act_tab``, which runs it under ``_lock``). Uses the
        lock-free ``_set_active_page_locked`` since the caller already holds ``_lock`` --
        re-taking it (via ``_set_active_page``) would deadlock the browser."""
        if self._context is None:
            return
        action = message.get("action")
        # ``switch`` is the CLI's word for activating an existing tab (agentic-browser-fleet
        # SKILL: `tab <name> switch <index>`); treat it as ``activate``.
        if action in ("activate", "switch"):
            index = int(message.get("index", 0))
            if 0 <= index < len(self._context.pages):
                page = self._context.pages[index]
                await page.bring_to_front()
                await self._set_active_page_locked(page)
        elif action == "new":
            page = await self._context.new_page()
            await page.goto(message.get("url") or _HOME_URL)
        elif action == "close":
            index = int(message.get("index", 0))
            if 0 <= index < len(self._context.pages):
                # Closing the LAST tab would close the window and take the whole browser
                # down with it -- but a browser is only ever retired via the explicit
                # `close` command, not by emptying its tabs. So open a fresh home tab
                # first (before closing) whenever this is the last one.
                if len(self._context.pages) == 1:
                    home = await self._context.new_page()
                    await home.goto(_HOME_URL)
                await self._context.pages[index].close()

    # --- clipboard bridge (human viewer <-> the browser's X11 clipboard) ------
    # Each browser runs headful under its OWN Xvfb display, so it has its own X11
    # clipboard (no shared-:99 cross-talk). xclip reads/writes that clipboard from
    # OUTSIDE the page and XTest fires the paste/copy, so both are fully native -- no
    # page-origin Async Clipboard API, no https/user-activation constraints, images
    # like text. Gated on _input_enabled: only the controlling human, never an agent.

    async def clipboard_paste(self, data: bytes, mime: str) -> dict[str, Any]:
        """Write the user's clipboard payload into the browser's X11 clipboard, then
        fire a native Ctrl+V (XTest) into the focused element. ``mime`` is text/* or
        image/*."""
        async with self._control_lock:
            if not self._input_enabled.is_set():
                return {"ok": False, "status": "not_controlling"}
            if self._display is None:
                return {"ok": False, "status": "no_display"}
            if not await self._xclip_write(data, mime):
                return {"ok": False, "status": "clipboard_error"}
            self._own_clip_hash = hash(data)  # so the copy-out watcher doesn't echo it
            self._clipboard_combo("KeyV", "v")
        return {"ok": True}

    async def clipboard_copy(self, *, cut: bool = False) -> dict[str, Any]:
        """Fire a native Ctrl+C / Ctrl+X (XTest) on the current selection, then read the
        X11 clipboard out for the user. Returns ``{ok, mime, text|data}``; ``mime`` is
        None when nothing is selected. Binary payloads come back base64 in ``data``,
        text in ``text``."""
        async with self._control_lock:
            if not self._input_enabled.is_set():
                return {"ok": False, "status": "not_controlling"}
            if self._display is None:
                return {"ok": False, "status": "no_display"}
            self._clipboard_combo("KeyX" if cut else "KeyC", "x" if cut else "c")
            data, mime = await self._xclip_read(settle=True)
            # Mark our own read INSIDE the lock: the copy-out poll runs lock-free and would
            # otherwise read this fresh Chromium-owned selection in the gap and echo it back.
            if data is not None:
                self._own_clip_hash = hash(data)
        if data is None or mime is None:
            return {"ok": True, "mime": None}  # nothing selected / empty clipboard
        if mime.startswith("text/"):
            return {"ok": True, "mime": mime, "text": data.decode("utf-8", "replace")}
        return {"ok": True, "mime": mime, "data": base64.b64encode(data).decode("ascii")}

    async def clipboard_peek(self) -> dict[str, Any]:
        """Read the CURRENT X clipboard WITHOUT firing a copy -- for a remote-initiated
        copy (right-click -> Copy) that the poll detected and only NOTIFIED the viewer of
        (an image is too big to inline over the cast socket). Returns ``{ok, mime,
        text|data}``; binary comes back base64 in ``data``."""
        async with self._control_lock:
            if not self._input_enabled.is_set():
                return {"ok": False, "status": "not_controlling"}
            if self._display is None:
                return {"ok": False, "status": "no_display"}
            data, mime = await self._xclip_read(settle=False)
        if data is None or mime is None:
            return {"ok": True, "mime": None}
        if mime.startswith("text/"):
            return {"ok": True, "mime": mime, "text": data.decode("utf-8", "replace")}
        return {"ok": True, "mime": mime, "data": base64.b64encode(data).decode("ascii")}

    def _clipboard_combo(self, code: str, char: str) -> None:
        """Ctrl+<key> at the display level (XTest), independent of the user's keymap."""
        d = self._display
        if d is None:
            return
        d.key("ControlLeft", "Control", True)
        d.key(code, char, True)
        d.key(code, char, False)
        d.key("ControlLeft", "Control", False)

    async def _clipboard_out_loop(self) -> None:
        """Push a copy that happened INSIDE the remote page (right-click -> Copy, or any
        app copy) to the viewer, so it reaches the user's local clipboard. Polls the X
        clipboard (~500ms is plenty for a clipboard) while a human controls a WATCHED
        browser, skipping the value we ourselves last wrote."""
        prev_enabled = True
        while not self._closed:
            await asyncio.sleep(_CLIPBOARD_POLL_SECONDS)
            enabled = self._input_enabled.is_set()
            if (
                self._display is None
                or not self._is_running
                or not enabled
                or self._capture is None
                or not self._capture.has_subscribers()
            ):
                prev_enabled = enabled
                continue
            data, mime = await self._xclip_read(settle=False)
            if data is None or mime is None:
                prev_enabled = enabled
                continue
            digest = hash(data)
            if not prev_enabled:
                # Just regained control from an agent: the X clipboard may hold whatever
                # the agent copied during its run. Prime _seen WITHOUT pushing, so we don't
                # clobber the user's local clipboard with the agent's leftover.
                self._seen_clip_hash = digest
                prev_enabled = True
                continue
            if digest == self._seen_clip_hash or digest == self._own_clip_hash:
                continue
            self._seen_clip_hash = digest
            if mime.startswith("text/"):
                # Text is small: inline it over the cast socket.
                self._broadcast({"type": "clipboard", "mime": mime, "data": data.decode("utf-8", "replace")})
            else:
                # An image's base64 can blow past the WS proxy's ~1 MiB per-message cap,
                # which closes the cast socket ("Reconnecting…"). Push only a NOTIFICATION;
                # the viewer fetches the bytes over HTTP (GET .../clipboard/peek).
                self._broadcast({"type": "clipboard", "mime": mime})

    def _xclip_base(self) -> list[str]:
        base = ["xclip"]
        if self._display is not None:
            base += ["-display", self._display.name]  # this browser's own X clipboard
        return base + ["-selection", "clipboard"]

    async def _xclip_write(self, data: bytes, mime: str) -> bool:
        """Load ``data`` into this display's X11 CLIPBOARD selection. xclip forks a
        background owner that serves the selection until another app claims it."""
        args = self._xclip_base()
        if not mime.startswith("text/"):
            args += ["-t", mime]
        args += ["-i"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await _communicate_bounded(proc, data)
            return proc.returncode == 0
        except (OSError, TimeoutError) as e:
            logger.warning("xclip write failed for browser {} ({})", self.browser_id, e)
            return False

    async def _xclip_read(self, *, settle: bool) -> tuple[bytes | None, str | None]:
        """Read the X11 CLIPBOARD selection, preferring an image if present. ``settle``
        waits briefly first so a just-issued copy command lands before we read."""
        if settle:
            await asyncio.sleep(0.12)
        targets = await self._xclip_out("TARGETS")
        mime = "image/png" if targets and b"image/png" in targets else "text/plain"
        data = await self._xclip_out(mime)
        if not data:
            return None, None
        return data, mime

    async def _xclip_out(self, target: str) -> bytes | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._xclip_base(), "-o", "-t", target,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await _communicate_bounded(proc)
            return out if proc.returncode == 0 else None
        except (OSError, TimeoutError) as e:
            logger.warning("xclip read failed for browser {} ({})", self.browser_id, e)
            return None

    # --- ownership state machine ----------------------------------------------

    def _state_tuple(self) -> tuple[ControlOwner, str | None, bool]:
        return (self.controller, self.owner_agent_id, self.human_pinned)

    async def _write_control_locked(
        self, to: ControlOwner, agent_id: str | None, agent_name: str | None, pinned: bool
    ) -> None:
        """The ONLY writer of control state. Caller must hold ``_control_lock``.

        Writes ``controller``/``owner_agent_id``/``human_pinned`` and ``_input_enabled``
        together (so the input gate can never disagree with the controller), then
        broadcasts the new state to every cast socket. The broadcast is now a plain
        synchronous fan-out (no ``await``), so the four-field write + broadcast run
        with no intervening yield -- the input gate can never be observed mid-write.
        Late joiners get the same state via ``register_cast_queue``'s initial seed.
        """
        self.controller = to
        self.owner_agent_id = agent_id
        self.owner_agent_name = agent_name
        self.human_pinned = pinned
        if to == "human":
            self._input_enabled.set()
        else:
            self._input_enabled.clear()
            # The human's input stream is cut mid-whatever-they-held: synthesize the
            # missing releases, or a held key AUTOREPEATS on the display forever (XTest
            # holds are sticky) and a held button becomes a permanent drag.
            if self._display is not None:
                self._display.release_all()
            self._lease_touched_at = time.monotonic()  # start the sticky-lease idle clock
            # Remember the size the agent starts at, so a human resize during a later
            # takeover can be reported back to it on resume (see _wake_agent).
            self._agent_render_w, self._agent_render_h = self._render_w, self._render_h
        self._broadcast(self._control_message())

    def _waiting_names(self) -> list[str]:
        """Display names of every agent queued for this browser: the resume queue
        (agents auto-queued when their command was rejected) first, then any
        connection-bound task/hold waiters."""
        names = [name or agent_id for (agent_id, name) in self._resume_queue]
        names += [w.agent_name or w.agent_id for w in self._wait_queue]
        return names

    def _control_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "type": "control",
            # The explicit lifecycle (init/running/crashed) the viewer renders off of:
            # init -> full "Starting browser…" overlay, running -> live page, crashed ->
            # crashed overlay. Carried on EVERY control broadcast so the viewer reacts
            # to each transition deterministically (not by guessing from frames).
            "lifecycle": self._lifecycle,
            "owner": self.controller,
            "owner_agent_id": self.owner_agent_id,
            "owner_name": self.owner_agent_name,
            "human_pinned": self.human_pinned,
            # Current render size, so the viewer sizes its canvas to the video (the
            # capture is _render_w x _render_h; the frame stripes fill exactly that).
            "resolution": [self._render_w, self._render_h],
            # Agents queued (monitor-and-wait) behind the current owner, in FIFO order.
            "waiting": self._waiting_names(),
        }
        # While an agent holds a sticky direct-control lease (not a connection-bound
        # task), tell the viewer how long it has been idle and when the idle-TTL will
        # auto-release it, so a watching human knows the browser will free itself.
        if self.controller == "agent" and self._agent_task is None and self._lease_touched_at:
            idle = time.monotonic() - self._lease_touched_at
            msg["idle_seconds"] = max(0, int(idle))
            msg["idle_release_seconds"] = max(0, int(_LEASE_IDLE_TTL - idle))
        return msg

    def _control_state(self) -> dict[str, Any]:
        """Owner snapshot embedded in every direct-command response so the agent can
        tell, after each call, whether it still holds control (e.g. a human took it).
        Carries the lifecycle too, so a caller acting on an ``init`` browser sees why
        the command was deferred."""
        return {
            "lifecycle": self._lifecycle,
            "controller": self.controller,
            "owner_agent_id": self.owner_agent_id,
            "owner_name": self.owner_agent_name,
            "human_pinned": self.human_pinned,
            "resolution": [self._render_w, self._render_h],
        }

    async def acquire_with_state(
        self,
        agent_id: str,
        agent_name: str | None = None,
        *,
        reclaim: bool = False,
        wait: bool = True,
        max_wait: float | None = None,
        enqueue_on_busy: bool = False,
    ) -> dict[str, Any]:
        """:meth:`acquire`, then snapshot the control state -- both ON the loop so the
        snapshot reflects the post-acquire ownership atomically.

        The runner's ``cmd_acquire`` reads ``_control_state()`` after acquiring; reading
        it on the Flask thread would observe loop-mutated fields without going through the
        bridge (a torn/stale view). Returning ``{ok, status, **control_state}`` from one
        coroutine keeps that read on the loop thread where every mutation also happens."""
        status = await self.acquire(
            agent_id, agent_name, reclaim=reclaim, wait=wait, max_wait=max_wait, enqueue_on_busy=enqueue_on_busy
        )
        return {
            "ok": status == "acquired",
            "status": status,
            # Only promise a resume in the CLI when the agent was actually enrolled (see run_action).
            "enqueued": enqueue_on_busy and status in ("busy_human", "busy_agent"),
            **self._control_state(),
        }

    async def handoff_with_state(self, agent_id: str, agent_name: str | None, reason: str) -> dict[str, Any]:
        """:meth:`handoff`, then snapshot the control state -- both ON the loop (see
        :meth:`acquire_with_state`), so the runner's ``cmd_handoff`` never reads
        loop-mutated ownership fields off the Flask thread."""
        handed = await self.handoff(agent_id, agent_name, reason)
        status = "handed_off" if handed else "not_owner"
        return {"ok": handed, "status": status, **self._control_state()}

    def _enqueue_resume_locked(self, agent_id: str, agent_name: str | None) -> None:
        """Add an agent to the resume queue (deduped by id). Caller holds _control_lock."""
        if not any(aid == agent_id for (aid, _) in self._resume_queue):
            self._resume_queue.append((agent_id, agent_name))

    def _enqueue_resume_front_locked(self, agent_id: str, agent_name: str | None) -> None:
        """Put an agent at the FRONT of the resume queue -- it handed off mid-task (e.g. a
        CAPTCHA), so it resumes before agents that were merely waiting their turn. Moves
        an existing entry to the front. Caller holds _control_lock."""
        self._resume_queue = [(aid, an) for (aid, an) in self._resume_queue if aid != agent_id]
        self._resume_queue.insert(0, (agent_id, agent_name))

    def _dequeue_resume_locked(self, agent_id: str) -> None:
        """Drop an agent from the resume queue (it took control / no longer waiting)."""
        self._resume_queue = [(aid, an) for (aid, an) in self._resume_queue if aid != agent_id]

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run a fire-and-forget coroutine, holding a strong ref so it isn't GC'd."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _spawn_wake(self, agent_id: str, agent_name: str | None, baseline: tuple[int, int]) -> None:
        """Schedule a wake, holding a strong ref so the task isn't GC'd before it runs.
        ``baseline`` is the render size the agent last saw (captured BEFORE re-granting),
        so the wake can tell whether the human resized meanwhile."""
        self._spawn(self._wake_agent(agent_id, agent_name, baseline))

    def _on_disconnected(self, _browser: Browser | None) -> None:
        """Playwright fires this when the Chromium CDP connection drops. During our own
        teardown (``_closed``) it's expected; otherwise the browser crashed -- record it
        and tell the viewer. The agent finds out on its next command (see run_action).

        The ``_browser`` argument is the Playwright callback's payload and is unused; it
        is typed ``| None`` because the lazy crash-detection path in ``run_action`` calls
        this with ``self._observer``, which may already be ``None`` when the connection
        is gone (and tests invoke it with ``None`` directly)."""
        if self._closed or self._crashed:
            return
        self._crashed = True
        self._spawn(self._announce_crash())

    async def _announce_crash(self) -> None:
        logger.warning("browser {} crashed (Chromium connection lost)", self.browser_id)
        # Stop the encoder against the dead browser and send every /stream viewer the
        # shutdown sentinel -- otherwise the capture keeps running (and viewers hold
        # open, frozen sockets) until each happens to disconnect on its own.
        if self._capture is not None:
            self._capture.close()
        if self._audio is not None:
            self._audio.close()
        # Free the crash-orphaned HEAVY resources too: the Xvfb display + XTest connection,
        # this browser's PulseAudio sink, and the background tasks. The shell stays registered
        # (name + lifecycle="crashed") so the user can see it and close it, but nothing needs
        # the display/audio/tasks once Chromium is gone -- and a crashed shell does NOT count
        # toward the fleet cap, so without this a user could accumulate leaked Xvfb processes
        # and sinks, compounding the very memory pressure that usually caused the crash. close()
        # (if the user later closes the shell) is idempotent against this partial teardown.
        for task in (self._keepalive_task, self._clip_watch_task):
            if task is not None:
                task.cancel()
        if self._audio_sink_module is not None:
            remove_null_sink(self._audio_sink_module)
            self._audio_sink_module = None
        if self._display is not None:
            await self._display.close()
            self._display = None
        self._broadcast({"type": "crashed", "browser_id": self.browser_id})
        # Release anyone queued for this browser: it will never free, so wait-queue waiters
        # must not hang and resume-queue agents must be told rather than wait for a wake
        # that never comes. (close() does the same for a user-closed browser.)
        async with self._control_lock:
            await self._abandon_queues_locked("crashed")
        if self._crash_save_hook is not None:
            # Drop the dead browser from the manifest now (it's excluded from the live
            # snapshot), so a kill right after the crash doesn't restore it as healthy.
            self._crash_save_hook()

    def _observer_alive(self) -> bool:
        """Whether the Chromium connection is still up (cheap, no round-trip)."""
        return self._observer is not None and self._observer.is_connected()

    def _crashed_payload(self) -> dict[str, Any]:
        return {"ok": False, "status": "crashed", **self._control_state()}

    def _starting_payload(self) -> dict[str, Any]:
        """Non-fatal "the browser is still launching" response for a command that arrives
        while the browser is still ``init`` (Chromium not up yet). The CLI maps this to
        the same wait-and-retry path as the fleet-still-restoring 503, so the agent waits
        rather than erroring out."""
        return {"ok": False, "status": "starting", **self._control_state()}

    async def _message_agent(self, agent_id: str, agent_name: str | None, text: str) -> None:
        """Best-effort: message a queued agent via ``mngr message`` (the same path
        launch-task uses). Failures are logged, not raised -- the claim window / lifecycle
        handling is the backstop if a message never lands.

        These are automated, non-human nudges, so the text is wrapped in the
        ``_SYSTEM_MESSAGE_TAG`` sentinel: the transcript UI recognises it and
        renders a collapsed system chip instead of a bare user bubble. This is
        display-only -- the agent still receives the message and resumes its turn
        exactly as before."""
        target = agent_name or agent_id
        wrapped = _wrap_system_message(text)
        try:
            proc = await asyncio.create_subprocess_exec(
                "mngr",
                "message",
                target,
                "--message",
                wrapped,
                # Run from the repo root so the `mngr` dev shim resolves this checkout
                # (repo-relative paths assume cwd = repo root; don't rely on the
                # daemon's inherited cwd).
                cwd=str(_repo_root()),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except OSError as e:
            logger.warning("could not message agent {} for browser {} ({})", target, self.browser_id, e)

    async def _wake_agent(self, agent_id: str, agent_name: str | None, baseline: tuple[int, int]) -> None:
        """Message a queued agent that the browser is its again, so it resumes in a
        fresh turn (it ended its turn when it lost control). If it fails, or the agent
        never shows, the claim window passes the browser on.

        ``baseline`` is the size the agent last saw, captured by ``_settle_queue_locked``
        BEFORE the re-grant -- comparing against ``self._agent_render_*`` here would always
        say "unchanged", since the re-grant's ``_write_control_locked`` just reset those to
        the current size."""
        if (self._render_w, self._render_h) != baseline:
            size_note = (
                f" The view is now {self._render_w}x{self._render_h} "
                f"(was {baseline[0]}x{baseline[1]} when you left) -- the page reflowed, "
                f"so your earlier element numbers are void; recompute from the fresh `state` list."
            )
        else:
            size_note = f" The view is {self._render_w}x{self._render_h} (unchanged)."
        await self._message_agent(
            agent_id,
            agent_name,
            f"Browser {self.browser_id} was handed back to you (the human finished with it). "
            f"Re-run `state {self.browser_id}` to re-read the page, then continue where you left off."
            f"{size_note}",
        )

    async def _abandon_queues_locked(self, reason: str) -> None:
        """The browser is gone (crashed or closed): release EVERY queued agent so none
        waits on a corpse. Caller holds ``_control_lock``.

        * ``_wait_queue`` (connection-bound task/hold waiters) are woken ungranted -> their
          ``acquire`` falls through to the crashed/closed check and returns that status, so
          the streaming endpoint ends with a clear "gone" instead of hanging forever on a
          browser that will never free.
        * ``_resume_queue`` agents ended their turn waiting to be MESSAGED when it frees;
          it never will, so message each that it's gone and clear the queue -- otherwise
          they wait forever for a wake that never comes.
        """
        waiters, self._wait_queue = self._wait_queue, []
        for waiter in waiters:
            waiter.granted = False
            waiter.event.set()
        resume, self._resume_queue = self._resume_queue, []
        for agent_id, agent_name in resume:
            self._spawn(
                self._message_agent(
                    agent_id,
                    agent_name,
                    f"Browser {self.browser_id} is gone ({reason}) and won't come back. "
                    f"Start a new browser with `new` if you still need one.",
                )
            )

    async def _settle_queue_locked(self) -> None:
        """Reconcile both wait-queues with the current control state. Holds ``_control_lock``.

        * human-pinned -> evict the connection-bound ``_wait_queue`` (task/hold waiters
          never block on a human); the resume queue PERSISTS -- those agents want the
          browser back *after* the human is done.
        * free (unpinned human) -> hand the browser to the first waiter: a live
          ``_wait_queue`` waiter if any, else the first ``_resume_queue`` agent, which
          is messaged to resume (it ended its turn) and put on the claim clock.
        * agent-owned -> nothing (someone holds it; queues stay put).
        """
        if self.controller == "human" and self.human_pinned:
            waiters, self._wait_queue = self._wait_queue, []
            for waiter in waiters:
                waiter.granted = False
                waiter.event.set()
            return
        if self.controller != "human":
            return
        if self._wait_queue:
            waiter = self._wait_queue.pop(0)
            # An agent can be in BOTH queues (it sent a direct command -> resume queue,
            # then ran `task`/`acquire --wait` -> wait queue). Granting it here must
            # also clear its resume-queue entry, or a later settle would re-grant the
            # freed browser to an agent that's already done and spuriously wake it.
            self._dequeue_resume_locked(waiter.agent_id)
            await self._write_control_locked("agent", waiter.agent_id, waiter.agent_name, pinned=False)
            waiter.granted = True
            waiter.event.set()
        elif self._resume_queue:
            agent_id, agent_name = self._resume_queue.pop(0)
            # Capture the size the agent last saw BEFORE the re-grant -- _write_control_locked
            # resets _agent_render_* to the current size, so the wake must compare against
            # this snapshot to detect a human resize.
            baseline = (self._agent_render_w, self._agent_render_h)
            await self._write_control_locked("agent", agent_id, agent_name, pinned=False)
            self._granted_at = time.monotonic()  # start the claim window
            self._spawn_wake(agent_id, agent_name, baseline)

    async def _transition(
        self,
        *,
        to: ControlOwner,
        agent_id: str | None = None,
        agent_name: str | None = None,
        pinned: bool = False,
        expect: tuple[ControlOwner, str | None, bool] | None = None,
        preempt: bool = False,
    ) -> bool:
        """Atomic compare-and-set control transition (the single mutation path).

        Returns False (and changes nothing) if ``expect`` is given and the current
        state differs -- this is how a stale finally / double-release no-ops safely.
        When ``preempt`` is set, the displaced agent's run is cancelled OUTSIDE the
        lock and never awaited here: the cancelled run's own finally re-enters this
        method, CAS-fails (state already moved on), and no-ops -- so there is no
        lock cycle (the deadlock the audit warned about).
        """
        displaced_agent: Agent | None = None
        displaced_task: "asyncio.Task[None] | None" = None
        displaced_action: "asyncio.Task[Any] | None" = None
        async with self._control_lock:
            if expect is not None and self._state_tuple() != expect:
                return False
            if preempt:
                displaced_agent = self._agent
                displaced_task = self._agent_task
                displaced_action = self._direct_action_task  # a single in-flight command
                # A human taking control of a browser an agent is DRIVING queues that agent
                # at the FRONT of the resume queue, so it resumes first when the human hands
                # back -- regardless of what it runs next. Without this, a preempted agent
                # whose next command is the read-only `state` re-check (which deliberately
                # does NOT enrol a waiter) would be silently dropped: told "you're queued"
                # while in no queue, never woken on hand-back, and not shown in the human's
                # waiting list (so the "Return control to agents" button never appears).
                # Mirrors the agent-initiated handoff; the human-pinned settle below keeps
                # the resume queue intact.
                if self.controller == "agent" and self.owner_agent_id is not None:
                    self._enqueue_resume_front_locked(self.owner_agent_id, self.owner_agent_name)
            await self._write_control_locked(to, agent_id, agent_name, pinned)
            await self._settle_queue_locked()
        if displaced_agent is not None:
            displaced_agent.stop()
        for task in (displaced_task, displaced_action):
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
        return True

    async def acquire(
        self,
        agent_id: str,
        agent_name: str | None = None,
        *,
        reclaim: bool = False,
        wait: bool = True,
        max_wait: float | None = None,
        enqueue_on_busy: bool = False,
        on_wait: Callable[[str | None, str | None], Awaitable[None]] | None = None,
    ) -> str:
        """Acquire control for an agent. Returns one of:

        ``"acquired"`` -- the agent now controls the browser.
        ``"busy_human"`` -- a human took control (pinned); it stays the human's until
            they hand back. Only an explicit ``reclaim`` takes it. A *resting* human
            (not pinned) is free and taken.
        ``"busy_agent"`` -- another agent holds it and ``wait`` was False.
        ``"timed_out"`` -- waited ``max_wait`` seconds for another agent to release.
        ``"starting"`` -- the browser is still launching (``init``); driving/ownership
            only applies once running. Non-fatal -- the caller waits and retries.
        ``"crashed"`` -- Chromium died; the browser is gone.

        With ``wait`` (the default) and another agent in control, the caller parks in
        a FIFO queue and is handed the browser the instant that agent releases.

        With ``enqueue_on_busy`` (the direct-control path), a ``busy_human`` /
        ``busy_agent`` result also adds the agent to the resume queue: it ended its
        turn, and the daemon will message it to resume when the browser frees.
        """
        # Ownership/driving only applies once the browser is running. An init browser
        # has no Chromium yet (and no _bu_session to drive); a crashed one is gone. Both
        # are reported here so task/hold/acquire don't park a waiter on (or try to drive)
        # a browser that can't be driven. run_action gates on lifecycle before it calls
        # acquire, so this is the guard for the task/hold/explicit-acquire paths.
        # _closed FIRST and separately from _is_running: a request can resolve this browser
        # object just before a concurrent close() tears it down and cancels the keepalive
        # loop (which is the only thing that would ever re-settle the queue). Without this
        # guard such a request appends a waiter that nothing will ever wake -- hanging the
        # Flask worker forever on a corpse. A closed browser is gone: never queue on it.
        if self._closed:
            return "closed"
        if self._crashed:
            return "crashed"
        if not self._is_running:
            return "starting"
        async with self._control_lock:
            if self.controller == "agent" and self.owner_agent_id == agent_id:
                self.owner_agent_name = agent_name  # refresh display name on re-acquire
                self._dequeue_resume_locked(agent_id)
                # Count this as claiming a pending grant: an agent woken to resume that
                # claims via `acquire` (not a direct command) must clear the claim window
                # and touch the lease, or _sweep_unclaimed_grant revokes its grant ~12s
                # later and it loses its fronted CAPTCHA-resume slot.
                self._granted_at = 0.0
                self._lease_touched_at = time.monotonic()
                return "acquired"
            # ``reclaim`` deliberately overrides a human pin for ANY agent, not just the
            # displaced owner: it is the "the human told me to keep going / take over" verb,
            # and the daemon cannot verify which agent the human addressed. This is an
            # intentional trust assumption (cooperative agents following the skill, which
            # says to reclaim ONLY on an explicit user instruction), not an oversight -- the
            # human's own take-control always wins again instantly if they disagree.
            if not reclaim and self._human_pin_active():
                if enqueue_on_busy:
                    self._enqueue_resume_locked(agent_id, agent_name)
                    self._broadcast(self._control_message())
                return "busy_human"
            if self.controller == "human":  # free, a stale pin, or reclaim of a pin
                self._dequeue_resume_locked(agent_id)
                await self._write_control_locked("agent", agent_id, agent_name, pinned=False)
                return "acquired"
            # controller == "agent", a different agent -> must wait or fail fast.
            if not wait:
                if enqueue_on_busy:
                    self._enqueue_resume_locked(agent_id, agent_name)
                    self._broadcast(self._control_message())
                return "busy_agent"
            busy_id, busy_name = self.owner_agent_id, self.owner_agent_name
            waiter = _AcquireWaiter(agent_id, agent_name)
            self._wait_queue.append(waiter)
        if on_wait is not None:
            await on_wait(busy_id, busy_name)
        try:
            await asyncio.wait_for(waiter.event.wait(), timeout=max_wait)
        except (TimeoutError, asyncio.CancelledError) as exc:
            async with self._control_lock:
                if waiter in self._wait_queue:
                    self._wait_queue.remove(waiter)
                elif waiter.granted and self.controller == "agent" and self.owner_agent_id == agent_id:
                    # Handed the browser concurrently with our give-up: release it so
                    # the next waiter (or the human) isn't blocked by a no-show owner.
                    await self._write_control_locked("human", None, None, pinned=False)
                    await self._settle_queue_locked()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return "timed_out"
        # The browser may have died while we were parked: crash/close evicts the wait queue
        # ungranted, so report that (not a misleading "busy_human") and the agent starts fresh.
        if self._crashed:
            return "crashed"
        if self._closed:
            return "closed"
        if waiter.granted:
            return "acquired"
        # Evicted because a human took control while we waited. If the caller asked to be
        # resumed (enqueue_on_busy -- task/hold do), enrol it in the resume queue NOW: the
        # fail-fast enqueue branches never ran for a parked waiter, so without this the
        # caller is told "you're queued, we'll message you" but is in no queue and is never
        # woken on hand-back.
        if enqueue_on_busy:
            async with self._control_lock:
                self._enqueue_resume_locked(agent_id, agent_name)
                self._broadcast(self._control_message())
        return "busy_human"

    async def release(self, agent_id: str) -> bool:
        """Release this agent's control back to the human (free). CAS: only the owner can."""
        return await self._transition(to="human", expect=("agent", agent_id, False))

    async def take_control(self) -> bool:
        """Human 'take control': preempt whatever agent is driving and pin (agents locked out).

        Always wins (no ``expect``): flips to a pinned human and cancels the run. The
        cancel happens outside the control lock, so the run's finally can re-enter the
        state machine without deadlocking. The pin is sticky -- it holds until the human
        explicitly hands back via :meth:`return_to_agents`, with no idle/grace yield (a
        human who took control keeps it even if they step away).

        Gated on lifecycle (like :meth:`acquire` / :meth:`run_action`): ownership only
        applies once the browser is ``running``. Taking control of an ``init`` browser
        would pin it before Chromium is even up, so it would come up locked to the human
        and block every agent; a ``crashed`` browser is gone. In both cases this no-ops
        (returns False) -- the human can take control once it's live. Returns True when
        the pin landed.
        """
        if not self._is_running:
            return False
        return await self._transition(to="human", pinned=True, preempt=True)

    async def handoff(self, agent_id: str, agent_name: str | None, reason: str) -> bool:
        """Agent-initiated handoff to the human (e.g. a CAPTCHA / verification it can't
        solve). Atomically, if the caller currently holds the browser: put it at the
        FRONT of the resume queue (it's mid-task), then hand control to the human PINNED.
        Control goes to the *human* -- not the next queued agent -- and stays there until
        the human explicitly returns it (the sticky pin), at which point this requester is
        the first agent woken to resume. Returns False (no change) if the caller doesn't
        hold it (a human already took over, or its lease lapsed).
        """
        async with self._control_lock:
            if not (self.controller == "agent" and self.owner_agent_id == agent_id):
                return False
            self._enqueue_resume_front_locked(agent_id, agent_name)
            await self._write_control_locked("human", None, None, pinned=True)
            await self._settle_queue_locked()  # evict any connection-bound task/hold waiters
            self._broadcast(
                {
                    "type": "handoff_request",
                    "browser_id": self.browser_id,
                    "agent_name": agent_name or agent_id,
                    "reason": reason,
                    "url": self._active_page.url if self._active_page is not None else None,
                    **self._control_state(),
                }
            )
        return True

    async def return_to_agents(self) -> bool:
        """Human hands control back: un-pin (only if currently pinned). Frees any waiter."""
        return await self._transition(to="human", pinned=False, expect=("human", None, True))

    async def run_agent(self, agent_id: str, prompt: str, on_event: EventSink) -> None:
        """Run a browser-use task against this (already-acquired) browser, streaming steps.

        The caller (the task endpoint) acquires the browser in one submitted coroutine
        and submits this run as a SEPARATE coroutine; between the two the loop is free to
        run a human ``take_control`` (or an idle-lease sweep). So registering this run's
        cancellable handle (``_agent_task``/``_agent``) MUST be atomic with ownership:
        we take ``_control_lock`` and re-check that ``agent_id`` still owns the browser
        (``controller == "agent"`` and ``owner_agent_id == agent_id``, unpinned) BEFORE
        registering the handle and driving. This mirrors the pre-refactor design, where
        acquire and the ``run_agent`` task lived in one coroutine on the loop with no
        intervening preemption -- the invariant being that the cancellable handle and
        ownership move together.

        If ownership was lost in that gap (a human preempted, or the lease was swept),
        we emit ``lost_control`` and return WITHOUT touching the browser -- we never
        drive a browser the human (or another agent) now owns. Once the handle is
        registered under the lock, a subsequent ``take_control`` sees ``_agent_task``
        and cancels this run via the bridge.
        """
        api_key = resolve_anthropic_key()
        if not api_key:
            await on_event({"type": "error", "text": anthropic_key_status()[1]})
            return
        # Key is passed straight to ChatAnthropic -- never into os.environ, which would
        # leak across the manager's concurrent sessions and race between runs. Build the
        # Agent BEFORE taking the lock (it mutates no shared state); only the handle
        # registration below must be atomic with the ownership re-check.
        agent = Agent(
            task=prompt,
            llm=ChatAnthropic(model=_DEFAULT_MODEL, api_key=api_key),
            browser_session=self._bu_session,
        )
        async with self._control_lock:
            if self._state_tuple() != ("agent", agent_id, False):
                # A human took control (or the lease was swept) between the caller's
                # acquire and this run starting. Do not register the handle or drive --
                # the browser is no longer ours.
                await on_event({"type": "lost_control", **self._control_state()})
                return
            self._run_on_event = on_event
            self._agent_task = asyncio.current_task()
            self._agent = agent
        try:
            await asyncio.wait_for(
                agent.run(on_step_end=self._on_agent_step, max_steps=_TASK_MAX_STEPS),
                timeout=_TASK_MAX_SECONDS,
            )
            summary = agent.history.final_result()
            await on_event({"type": "done", "result": summary or "Done."})
        except asyncio.CancelledError:
            # Distinguish a human take_control (resume on hand-back) from the browser being
            # closed/gone (no resume will ever come) -- otherwise the agent is told "you were
            # preempted" and waits forever for a browser that no longer exists.
            if self._closed or self._crashed:
                await on_event({"type": "error", "text": f"Browser {self.browser_id} was closed."})
            else:
                await on_event({"type": "preempted"})
            raise
        except TimeoutError:
            agent.stop()
            await on_event({"type": "error", "text": f"Task exceeded {_TASK_MAX_SECONDS:.0f}s and was stopped."})
        except Exception as e:  # noqa: BLE001 -- surface any agent failure to the caller's stream
            logger.opt(exception=e).error("browser-use agent run failed for browser {}", self.browser_id)
            await on_event({"type": "error", "text": f"Agent error: {e}"})
        finally:
            if self._agent is agent:
                self._agent = None
                self._agent_task = None
                self._run_on_event = None

    async def _on_agent_step(self, agent: Agent) -> None:
        """browser-use per-step hook: stream the latest thought + action as separate events."""
        emit = self._run_on_event
        if emit is None:
            return
        history = agent.history
        thoughts = history.model_thoughts()
        actions = history.model_actions()
        if thoughts:
            thought = thoughts[-1]
            summary = str(
                getattr(thought, "next_goal", "")
                or getattr(thought, "evaluation_previous_goal", "")
                or "Thinking"
            ).strip()
            detail = str(getattr(thought, "thinking", "") or thought).strip()
            await emit({"type": "thinking", "text": summary, "detail": detail})
        if actions:
            action = actions[-1]
            await emit(
                {"type": "action", "text": _action_summary(action), "detail": json.dumps(action, indent=2, default=str)}
            )
        # Keep the streamed view on whatever tab the agent is now focused on.
        await self._follow_agent_focus()

    async def _stop_active_agent(self) -> None:
        """Stop any running agent and wait for its run task to unwind (used by close())."""
        agent = self._agent
        task = self._agent_task
        if agent is not None:
            agent.stop()
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, *_BROWSER_ERRORS):
                pass

    # --- direct control (Claude drives the browser itself, one command at a time) ---

    def _ensure_action_handler(self) -> ActionHandler:
        """browser-use's own action executor, bound once to our held BrowserSession."""
        if self._action_handler is None:
            self._action_handler = ActionHandler(self._bu_session)
        return self._action_handler

    async def run_action(
        self,
        agent_id: str,
        agent_name: str | None,
        action: Callable[[ActionHandler], Awaitable[dict[str, Any]]],
        enqueue_on_busy: bool = True,
    ) -> dict[str, Any]:
        """Run one direct-control action for an agent, returning a result + owner snapshot.

        ``enqueue_on_busy`` (default True) queues the agent to resume when a busy browser
        frees. The read-only ``state`` passes False: merely *looking* at a browser a
        human/another agent is driving must not silently enrol the agent as a waiter.

        Ownership is a sticky lease: the first action acquires the browser (CAS, no
        wait -- a busy browser fails fast rather than blocking a click), later actions
        refresh it. The CRITICAL guard is the per-command compare-and-set: right before
        the browser action we re-check ``(agent, me, unpinned)`` under ``_control_lock``,
        so a human take-control between two commands makes the next one a clean no-op
        (``lost_control``) instead of touching the human's browser. The action itself
        runs under ``_lock`` (serialized with screencast tab-switches), NOT under
        ``_control_lock`` -- so a human take-control stays instant (at worst one
        in-flight action lands before the next command sees it).
        """
        # Closed out from under us (a concurrent DELETE resolved after this request grabbed
        # the browser object): don't acquire/drive/queue on a torn-down browser -- its
        # keepalive loop is cancelled, so a queued waiter would hang forever.
        if self._closed:
            return {"ok": False, "status": "closed", **self._control_state()}
        # The browser died (OS/OOM kill, crash): don't try to acquire or drive a
        # corpse -- tell the agent it's gone so it starts a fresh one.
        if self._crashed:
            return self._crashed_payload()
        # Still launching (registered but Chromium not up yet): driving/ownership only
        # applies once running. Return a clear, non-fatal "starting" so the CLI/agent
        # waits and retries instead of erroring on a half-built browser.
        if not self._is_running:
            return self._starting_payload()
        # Did I already hold the lease, or does this command newly take the browser?
        # The client uses this to surface the browser pane exactly once -- on the
        # first command for a browser (and again after a human hands it back) --
        # rather than on every click.
        was_mine = self._state_tuple() == ("agent", agent_id, False)
        status = await self.acquire(agent_id, agent_name, wait=False, enqueue_on_busy=enqueue_on_busy)
        if status != "acquired":
            # ``enqueued`` tells the CLI whether the agent was actually enrolled to be woken
            # so it only promises "you're queued ... messaged when it frees" when true. The
            # read-only `state` peek passes enqueue_on_busy=False, so a busy `state` must NOT
            # over-promise a resume that will never come.
            return {
                "ok": False,
                "status": status,
                "enqueued": enqueue_on_busy and status in ("busy_human", "busy_agent"),
                **self._control_state(),
            }
        async with self._control_lock:
            if self._state_tuple() != ("agent", agent_id, False):
                # A human grabbed control in the tiny window between acquire and here.
                # Queue this agent to resume (same as the busy_human path) so the
                # daemon messages it back when the human hands the browser over -- but
                # only for state-changing commands, not a passive `state` peek.
                if enqueue_on_busy:
                    self._enqueue_resume_locked(agent_id, agent_name)
                    self._broadcast(self._control_message())
                return {"ok": False, "status": "lost_control", "enqueued": enqueue_on_busy, **self._control_state()}
            self._lease_touched_at = time.monotonic()
            self._granted_at = 0.0  # the agent claimed (sent a command); cancel the claim window
        self._direct_action_task = asyncio.current_task()
        try:
            async with self._lock:
                if self._context is None:
                    return {"ok": False, "status": "closed", **self._control_state()}
                result = await action(self._ensure_action_handler())
        except asyncio.CancelledError:
            # A human take_control preempted this in-flight action (see _transition). Don't
            # let the human's XTest overlap it -- report lost_control so the agent re-checks
            # ownership and resumes rather than treating this as a crash.
            return {"ok": False, "status": "lost_control", **self._control_state()}
        except _BROWSER_ERRORS as e:
            logger.debug("direct action failed on browser {} ({})", self.browser_id, e)
            # If the connection is gone, the browser crashed (the `disconnected` event may
            # not have fired yet) -- classify it so the agent gets a clear "crashed, start a
            # new one" rather than a raw CDP exception.
            if not self._observer_alive():
                self._on_disconnected(self._observer)  # idempotent: marks + announces once
                return self._crashed_payload()
            return {"ok": False, "status": "error", "error": str(e), **self._control_state()}
        finally:
            self._direct_action_task = None
        return {"ok": True, "status": "ok", "newly_acquired": not was_mine, **result, **self._control_state()}

    def _node(self, index: int) -> Any:
        """Resolve an element index from the last ``state`` snapshot to its DOM node."""
        return self._selector_map.get(index)

    async def act_state(self, agent_id: str, agent_name: str | None) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            summary = await handler.get_state()
            self._selector_map = dict(getattr(summary.dom_state, "selector_map", {}) or {})
            elements = summary.dom_state.llm_representation()
            return {"url": summary.url, "title": summary.title, "elements": elements, "tabs": await self._tab_list()}

        # state is a read-only peek: don't enqueue the agent as a waiter on a busy browser.
        return await self.run_action(agent_id, agent_name, _do, enqueue_on_busy=False)

    async def act_navigate(self, agent_id: str, agent_name: str | None, url: str) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            await handler.navigate(url)
            self._selector_map = {}  # page changed -- old element indices are void
            return {"navigated": url}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_click(self, agent_id: str, agent_name: str | None, index: int) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            node = self._node(index)
            if node is None:
                return {"ok": False, "status": "stale_index", "error": f"no element {index}; run `state` first (the page may have changed)"}
            await handler.click_element(node)
            self._selector_map = {}  # a click may navigate/mutate -- force a re-`state`
            return {"clicked": index}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_input(self, agent_id: str, agent_name: str | None, index: int, text: str) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            node = self._node(index)
            if node is None:
                return {"ok": False, "status": "stale_index", "error": f"no element {index}; run `state` first"}
            await handler.type_text(node, text)
            return {"typed_into": index}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_select(self, agent_id: str, agent_name: str | None, index: int, value: str) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            node = self._node(index)
            if node is None:
                return {"ok": False, "status": "stale_index", "error": f"no element {index}; run `state` first"}
            await handler.select_dropdown(node, value)
            return {"selected": value, "index": index}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_scroll(self, agent_id: str, agent_name: str | None, direction: str, amount: int) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            await handler.scroll(direction, amount)
            self._selector_map = {}
            return {"scrolled": direction}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_keys(self, agent_id: str, agent_name: str | None, keys: str) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            await handler.send_keys(keys)
            return {"keys": keys}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_screenshot(self, agent_id: str, agent_name: str | None) -> dict[str, Any]:
        async def _do(_handler: ActionHandler) -> dict[str, Any]:
            data = await self._bu_session.take_screenshot()
            raw = data if isinstance(data, (bytes, bytearray)) else base64.b64decode(data)
            _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            self._screenshot_seq += 1
            path = _SCREENSHOT_DIR / f"browser-{self.browser_id}-{self._screenshot_seq}.png"
            path.write_bytes(raw)
            return {"screenshot_path": str(path.resolve())}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_tab(self, agent_id: str, agent_name: str | None, action: str, index: int | None, url: str | None) -> dict[str, Any]:
        async def _do(_handler: ActionHandler) -> dict[str, Any]:
            # Tabs go through OUR Playwright context, so the video follows the switch.
            # "list" is a read-only no-op here (the tab list is returned below); "switch"
            # is the CLI verb for activating a tab.
            if action in ("switch", "activate", "new", "close"):
                await self._handle_tab_control({"action": action, "index": index or 0, "url": url})
                self._selector_map = {}
            return {"tab_action": action, "tabs": await self._tab_list()}

        return await self.run_action(agent_id, agent_name, _do)

    # --- socket bookkeeping ---------------------------------------------------

    async def register_cast_queue(self) -> "queue.Queue[str | None]":
        """Register a new cast WebSocket and SEED its initial sync, atomically on the loop.

        Returns an outbound queue for the Flask cast handler to drain. The initial
        control + tabs (+ crash) sync is pushed BEFORE the queue is added to the fan-out
        list, so the viewer's first messages are deterministic. The video is a SEPARATE
        socket (``/stream``); a viewer that connects while a browser sits on a static
        page gets a fresh keyframe from the encoder (``request_idr_frame`` on subscribe),
        so there is no black-canvas replay to seed here.

        The seed is at most three messages onto a fresh, empty queue whose maxsize
        (``_CAST_QUEUE_MAX_SIZE`` = 16) is far larger, so the ``put_nowait``s here can
        never raise ``queue.Full``.

        Runs on the loop (the runner calls it via ``bridge.run``), so the list
        mutation is single-threaded with respect to :meth:`_broadcast`.
        """
        client_queue: "queue.Queue[str | None]" = queue.Queue(maxsize=_CAST_QUEUE_MAX_SIZE)
        # The control message carries the lifecycle, so the viewer's FIRST message tells
        # it whether to show the init overlay / live page / crashed overlay -- no guessing
        # from frames. tabs follow (empty until running).
        client_queue.put_nowait(json.dumps(self._control_message(), default=str))
        client_queue.put_nowait(json.dumps({"type": "tabs", "tabs": await self._tab_list()}, default=str))
        if self._crashed:  # a viewer opening a crashed browser sees the crash state at once
            client_queue.put_nowait(json.dumps({"type": "crashed", "browser_id": self.browser_id}, default=str))
        self._cast_queues.append(client_queue)
        return client_queue

    async def register_cast_queue_with_lifecycle(self) -> "tuple[queue.Queue[str | None], Lifecycle]":
        """:meth:`register_cast_queue`, returning the new queue AND the browser's lifecycle
        captured ON the loop in the same step.

        The runner uses the lifecycle to decide whether to push the fleet-level
        ``initializing`` banner: a viewer that joins an already-``running`` browser must
        NOT be told it's initializing (finding [3-runner]), even while the whole fleet is
        still restoring -- the seeded ``control`` already carries ``lifecycle=running`` and
        the live page is right there. Reading the lifecycle here (not on the Flask thread)
        keeps it consistent with the seed that was just built."""
        client_queue = await self.register_cast_queue()
        return client_queue, self._lifecycle

    async def unregister_cast_queue(self, client_queue: "queue.Queue[str | None]") -> None:
        """Remove a cast queue from the fan-out. Async so it runs ON the loop (via
        ``bridge.run``), keeping all ``_cast_queues`` list mutation single-threaded with
        respect to :meth:`_broadcast` -- no lock needed because the loop serializes it."""
        if client_queue in self._cast_queues:
            self._cast_queues.remove(client_queue)
        # Last viewer gone: a human who disconnected mid-drag / holding a key would leave
        # that button/key stuck down on the display (XTest holds are sticky). Release them.
        if not self._cast_queues and self._display is not None:
            self._display.release_all()

    async def describe(self) -> dict[str, Any]:
        """Snapshot for ``GET /browsers``: id, lifecycle, owner, and the tab list.

        ``lifecycle`` (init/running/crashed) is the explicit state the whole system
        reads; ``crashed`` is kept as a derived convenience for existing consumers (the
        CLI ``ls`` owner label). A browser still in ``init`` has no Chromium yet, so its
        tab list is empty (the round-trip would have nothing to read)."""
        return {
            "id": self.browser_id,
            "lifecycle": self._lifecycle,
            "controller": self.controller,
            "owner_agent_id": self.owner_agent_id,
            "owner_name": self.owner_agent_name,
            "human_pinned": self.human_pinned,
            "waiting": self._waiting_names(),
            "crashed": self._crashed,
            "tabs": [] if not self._is_running else await self._tab_list(),
        }

    def _broadcast(self, message: dict[str, Any]) -> None:
        """Fan a message out to every connected cast socket's outbound queue.

        Runs on the loop thread; pushes a JSON string onto each per-socket
        ``queue.Queue`` (thread-safe) for the owning Flask thread to send. On a
        full queue the client is behind, so we drop the OLDEST buffered frame and
        enqueue this one (a stale frame is worthless -- only the latest matters),
        mirroring WebSocketBroadcaster's drop-oldest policy.

        This is a plain ``def`` (no ``await``): it used to ``await ws.send_json``
        -- a real suspension point inside ``_write_control_locked`` while holding
        ``_control_lock`` -- and now only enqueues, which TIGHTENS the state
        machine's atomicity (one fewer mid-write yield). All call sites call it
        synchronously (no ``await``). The ``_cast_queues`` list is mutated only on
        this same loop thread (register/unregister go through the bridge), so
        iterating it here needs no lock.
        """
        text = json.dumps(message, default=str)
        for client_queue in self._cast_queues:
            try:
                client_queue.put_nowait(text)
            except queue.Full:
                try:
                    client_queue.get_nowait()  # drop the oldest buffered frame
                    client_queue.put_nowait(text)
                except (queue.Empty, queue.Full):
                    pass  # a concurrent drain raced us; the client will catch up

    def _shutdown_cast_queues(self) -> None:
        """Push the ``None`` shutdown sentinel onto every connected cast queue so each
        Flask cast thread tears down deterministically on the NEXT drain, instead of only
        when the client happens to disconnect. Runs on the loop (same as ``_broadcast``),
        so iterating ``_cast_queues`` needs no lock. Best-effort per queue: a full queue
        is drained once to make room for the sentinel (the client is going away anyway)."""
        for client_queue in self._cast_queues:
            try:
                client_queue.put_nowait(None)
            except queue.Full:
                try:
                    client_queue.get_nowait()  # make room; the client is shutting down
                    client_queue.put_nowait(None)
                except (queue.Empty, queue.Full):
                    pass

    async def close(self) -> None:
        self._closed = True
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
        if self._clip_watch_task is not None:
            self._clip_watch_task.cancel()
        # Tell every cast socket to tear down (don't wait for the client to disconnect).
        self._shutdown_cast_queues()
        # Stop the display encoder (and shut its stream sockets) before the display goes.
        if self._capture is not None:
            self._capture.close()
        # Stop the audio capture and unload this browser's PulseAudio sink.
        if self._audio is not None:
            self._audio.close()
        if self._audio_sink_module is not None:
            remove_null_sink(self._audio_sink_module)
            self._audio_sink_module = None
        # Release every queued agent so none hangs on a browser being torn down: wait-queue
        # waiters unblock (their acquire returns `closed`); resume-queue agents are messaged
        # it's gone and cleared.
        async with self._control_lock:
            await self._abandon_queues_locked("closed")
        await self._stop_active_agent()
        await self._detach_active_cdp()
        self._context = None  # bail out any nav re-attach queued during teardown
        if self._observer is not None:
            try:
                await self._observer.close()
            except _BROWSER_ERRORS as e:
                logger.debug("observer close ignored ({})", e)
        bu_session = getattr(self, "_bu_session", None)
        if bu_session is not None:
            try:
                await bu_session.kill()
            except _BROWSER_ERRORS as e:
                logger.debug("browser kill ignored ({})", e)
        # Kill this browser's Xvfb + XTest connection last, once nothing else needs it.
        if self._display is not None:
            await self._display.close()


async def _safe_title(page: Page) -> str:
    try:
        return await page.title()
    except _BROWSER_ERRORS:
        return page.url


class BrowserSessionManager(MutableModel):
    """Owns the whole fleet (all live browsers) and the shared Playwright driver.

    The fleet is shared per workspace: every agent in a mind reaches this one
    manager, so ``ls`` shows one fleet and ownership arbitrates between agents.
    Every browser is created on demand with a random ~2-word english NAME -- there
    is no default browser and the fleet starts EMPTY. Names are unique within the
    live fleet (generated under :attr:`_lock`, regenerated on collision) and never
    reused: a closed name is gone.

    REGISTER-INIT-IMMEDIATELY (the responsiveness fix): :meth:`create` registers a new
    :class:`LiveBrowser` in ``init`` under :attr:`_lock` (cap check + name resolution +
    add to ``_browsers``) and RETURNS at once, kicking the multi-second Chromium launch
    off as a background task. The route no longer blocks on the launch, so the
    optimistic viewer pane finds a real browser the instant it connects (the 1013
    "not-registered-yet" window shrinks to the sub-millisecond gap before the dict insert
    is visible).

    SERIALIZATION INVARIANT (the OOM guard): at most one Chromium is ``start()``-ing at a
    time. This is enforced by :attr:`_startup_lock` (a dedicated asyncio.Lock), which the
    background launch (:meth:`_launch`) holds across the WHOLE (multi-second) launch.
    Multiple ``init`` browsers queue on it and boot back-to-back, never in parallel.
    Registration (under :attr:`_lock`) is decoupled from launching (under
    :attr:`_startup_lock`): the cap counts ``init`` browsers too, so a flood of creates
    can't overshoot even though their launches run later. ``init`` browsers DO count
    toward the cap -- a half-started fleet still reserves its slots.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    _browsers: dict[str, LiveBrowser] = PrivateAttr(default_factory=dict)
    _playwright: Playwright | None = PrivateAttr(default=None)
    _lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    # Serializes the actual Chromium launches (the OOM guard). Decoupled from ``_lock``
    # (which serializes registry mutation): registration is instant, launching is slow,
    # so they take different locks. At most one launch runs at a time; ``init`` browsers
    # queue here and boot one after another. Strong refs to the in-flight launch tasks so
    # asyncio doesn't GC a bare create_task() result before it runs.
    _startup_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    _launch_tasks: set[Any] = PrivateAttr(default_factory=set)
    # Last manifest JSON written, so the periodic checkpoint is a no-op when nothing
    # changed (idle workspaces produce zero backup-branch churn).
    _last_manifest_json: str | None = PrivateAttr(default=None)
    _closed: bool = PrivateAttr(default=False)
    _checkpoint_task: "asyncio.Task[None] | None" = PrivateAttr(default=None)
    _bg_save_tasks: set[Any] = PrivateAttr(default_factory=set)  # strong refs for _spawn_save
    # Bounded ring of names whose background launch FAILED (finding [7]). A late/retrying
    # optimistic viewer that was in 1013 reconnect-backoff when the launch failed never
    # registered a cast queue, so it missed the launch_failed broadcast; the cast handler
    # consults this so such a name is closed 1008 (terminal) instead of looping on 1013.
    # ``deque(maxlen=...)`` auto-evicts the oldest, so this can't grow unbounded; mutated
    # only on the loop thread (the launch task + the cast resolve), so it needs no lock.
    _failed_launch_names: "deque[str]" = PrivateAttr(default_factory=lambda: deque(maxlen=_FAILED_LAUNCH_MEMORY))

    def _register_init_locked(self, name: str) -> LiveBrowser:
        """Construct a LiveBrowser in ``init`` and add it to the registry. Caller must
        hold ``self._lock``, so the cap check + name resolution + insert are atomic (no
        cap overshoot, no duplicate-name TOCTOU). Does NOT launch Chromium -- the caller
        kicks :meth:`_launch` off as a background task after releasing the lock."""
        session = LiveBrowser(browser_id=name)
        session._crash_save_hook = self._spawn_save  # checkpoint promptly if it crashes
        self._browsers[name] = session
        # A fresh registration supersedes any earlier launch-failure for this name (the
        # user re-created it, or restore is retrying it), so it's no longer terminal for a
        # viewer -- drop it from the failed ring so the cast handler stops 1008-ing it.
        self._clear_failed_launch(name)
        return session

    def _clear_failed_launch(self, name: str) -> None:
        """Forget a name's prior launch failure (it's being (re)launched). Mutated only on
        the loop thread, so no lock is needed."""
        if name in self._failed_launch_names:
            self._failed_launch_names = deque(
                (n for n in self._failed_launch_names if n != name), maxlen=_FAILED_LAUNCH_MEMORY
            )

    def recently_failed_launch(self, name: str) -> bool:
        """Whether ``name``'s last background launch failed (and it has not since been
        re-registered). The cast handler uses this to close a stale optimistic viewer
        terminally (1008) rather than telling it to retry (1013) forever (finding [7])."""
        return name in self._failed_launch_names

    async def recently_failed_launch_async(self, name: str) -> bool:
        """``recently_failed_launch`` for the cast handler to reach via ``bridge.run`` --
        running the ``_failed_launch_names`` read ON the loop thread (where the launch task
        mutates it) is what makes it race-free, like ``capacity_async``."""
        return self.recently_failed_launch(name)

    async def _launch(
        self, session: LiveBrowser, restore_tabs: list[str] | None = None, active_tab: int = 0, persist: bool = True
    ) -> None:
        """Serialized background Chromium launch for an already-registered ``init``
        browser. Holds :attr:`_startup_lock` across the WHOLE launch, so at most one
        Chromium starts at a time (the OOM guard) -- multiple ``init`` browsers queue here
        and boot back-to-back. On success ``session.start`` flips the lifecycle to
        ``running`` and broadcasts; on failure (Chromium never came up) we REMOVE the
        browser from the registry rather than leaving a stranded ``init`` shell -- an
        init that never launched would otherwise keep its name reserved and its cap slot
        forever, and (unlike a crash, which preserves a dead shell that the user explicitly
        closes) there is nothing for the user to look at. Runs entirely on the loop, so the
        registry mutation needs no extra lock.

        ``persist`` (default True for ``create``): checkpoint the manifest once the browser
        is running, since a new running browser is a topology change. Restore passes
        ``persist=False`` -- the post-restore reconcile owns the manifest there, and a
        per-launch save would race it and clobber the preserved-for-retry entries of
        browsers that flaked this boot.

        Playwright is started lazily here under ``_startup_lock``. On an empty-fleet fresh
        workspace this means Playwright is first started by the FIRST launch (restore
        launches nothing), not pre-warmed by restore -- harmless (same loop, no deadlock),
        just noted so the timing isn't surprising."""
        async with self._startup_lock:
            if session._closed or session.browser_id not in self._browsers:
                return  # closed (or already removed) while it sat in the launch queue
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            try:
                await session.start(self._playwright, restore_tabs=restore_tabs, active_tab=active_tab)
            except (BrowserStartupError, *_BROWSER_ERRORS) as e:
                logger.warning("browser {} failed to launch ({}); removing it", session.browser_id, e)
                self._browsers.pop(session.browser_id, None)
                # Remember the name as launch-failed (finding [7]) so a late/retrying
                # optimistic viewer -- one still in 1013 reconnect-backoff when this failed,
                # which never registered a cast queue and so missed the launch_failed
                # broadcast below -- is closed terminally (1008) instead of looping on 1013.
                self._failed_launch_names.append(session.browser_id)
                # Tell any viewer waiting on the optimistic pane that this name is gone
                # (terminal) BEFORE close() pushes the shutdown sentinel onto the cast
                # queues -- so the viewer sees the launch_failed message and then the
                # socket tears down deterministically, not only on its own disconnect.
                session._broadcast({"type": "launch_failed", "browser_id": session.browser_id})
                await session.close()  # don't leak a half-started Chromium; pushes the sentinel
                return
        # A new RUNNING browser is a topology change worth persisting promptly (create);
        # restore defers to its reconcile instead (persist=False).
        if persist:
            self._spawn_save()

    def _spawn_launch(
        self, session: LiveBrowser, restore_tabs: list[str] | None = None, active_tab: int = 0
    ) -> "asyncio.Task[None]":
        """Kick a serialized launch off as a background task, holding a strong ref so
        asyncio doesn't GC it before it runs. Records the task on the session so
        :meth:`close` can await it (serializing teardown against an in-flight launch).
        Returns the task (tests await it)."""
        task = asyncio.create_task(self._launch(session, restore_tabs=restore_tabs, active_tab=active_tab))
        session._launch_task = task
        self._launch_tasks.add(task)
        task.add_done_callback(self._launch_tasks.discard)
        task.add_done_callback(lambda _t: setattr(session, "_launch_task", None))
        return task

    async def create(self, name: str | None = None) -> LiveBrowser:
        """Start a new browser ('New browser' / fleet ``new``), optionally with a chosen name.

        Registers the browser in ``init`` under ``self._lock`` (cap check FIRST, then name
        resolution + insert -- all atomic) and RETURNS IMMEDIATELY, kicking the serialized
        Chromium launch off as a background task. The route returns ``{name}`` fast; the
        launch flips the browser to ``running`` (and broadcasts) when Chromium is up.

        Cap: ``init`` browsers COUNT toward the cap (a half-started fleet still reserves
        its slots); only crashed shells are excluded (they're dead, kept only to report
        "crashed"). A ``None`` name is generated and regenerated-on-collision against the
        registry (the uniqueness guarantee). A provided name is validated
        (:class:`InvalidBrowserNameError`) and rejected on collision
        (:class:`DuplicateBrowserNameError`).
        """
        async with self._lock:
            # Crashed browsers are dead shells kept only to report "crashed"; they
            # don't count toward the cap, so a crash never blocks opening a new one.
            # init + running both count -- the slot is reserved the moment we register.
            live = sum(1 for browser in self._browsers.values() if not browser._crashed)
            if live >= _MAX_SESSIONS:
                raise FleetFullError(f"{live}/{_MAX_SESSIONS} browsers open -- close one first.")
            if name is None:
                name = self._fresh_name_locked()
            else:
                if not is_valid_browser_name(name):
                    raise InvalidBrowserNameError(
                        f"'{name}' is not a valid browser name -- use lowercase letters, digits, and "
                        "single dashes (e.g. 'alex-smith'), 1-40 characters, no leading/trailing dash."
                    )
                if name in self._browsers:
                    raise DuplicateBrowserNameError(
                        f"the name '{name}' is already in use -- pick another, or close that browser first "
                        "(a crashed browser still holds its name until you close it)."
                    )
            session = self._register_init_locked(name)
        # Persist the manifest NOW, while the browser is still ``init`` (finding [5]):
        # the Chromium launch is multi-second, and a daemon crash in that window would
        # otherwise lose a browser the user just asked for. The init entry has no tabs
        # (it restores to home); the launch's own post-running save then captures its
        # real tabs. Fire-and-forget so create still returns immediately.
        self._spawn_save()
        self._spawn_launch(session)
        return session

    def _fresh_name_locked(self) -> str:
        """A generated name not currently in the live fleet. Caller holds ``self._lock``,
        so the check is against an unchanging ``_browsers`` -- this is the random-name
        uniqueness guarantee. Bounded so a pathological generator can't spin forever
        while holding the global launch lock (which would wedge the whole fleet): after
        a few dozen attempts, fall back to appending a short random suffix."""
        for _ in range(50):
            candidate = generate_browser_name()
            if candidate not in self._browsers:
                return candidate
        # Extremely unlikely (cap is 3); a defensive escape so we never loop unbounded.
        base = generate_browser_name()
        suffix = 1
        while f"{base}-{suffix}" in self._browsers:
            suffix += 1
        return f"{base}-{suffix}"

    def get(self, browser_id: str) -> LiveBrowser:
        # Dict access raises KeyError for a missing/closed name; callers turn it into a 404.
        return self._browsers[browser_id]

    async def resolve(self, browser_id: str) -> LiveBrowser:
        """:meth:`get` as a coroutine, so the sync web layer can resolve a browser ON the
        loop via ``bridge.run`` -- race-free against a concurrent close popping the name --
        without defining its own ``async def``. There is no default browser: every browser
        is created on demand and addressed by name; a closed/unknown name raises KeyError
        (-> 404) and is never reused."""
        return self.get(browser_id)

    def has_browser(self, browser_id: str) -> bool:
        return browser_id in self._browsers

    async def list_browsers(self) -> list[dict[str, Any]]:
        # Snapshot the browser OBJECTS up front (synchronous, one consistent view): each
        # describe() awaits CDP round-trips, and re-indexing self._browsers by name across
        # those awaits would KeyError if a concurrent close() popped a name (a human closing
        # a browser mid-`ls` -> HTTP 500). The snapshot keeps the closing browser's own
        # describe() valid (it reports its state) instead of exploding the whole list.
        snapshot = [self._browsers[name] for name in sorted(self._browsers)]
        return [await browser.describe() for browser in snapshot]

    async def close(self, browser_id: str) -> None:
        session = self._browsers.pop(browser_id, None)
        if session is None:
            return
        # Mark closed FIRST, then serialize against an in-flight launch: if create's
        # background _launch is suspended mid-start(), await it so the launch finishes (or
        # aborts via start()'s _abort_start_if_torn_down guard, which now sees _closed)
        # before we tear down -- otherwise a resuming start() could resurrect this removed
        # browser to "running" and leak a second Chromium. The launch holds _startup_lock
        # (not awaited here), so awaiting the task is the right join point.
        session._closed = True
        launch_task = session._launch_task
        if launch_task is not None and launch_task is not asyncio.current_task() and not launch_task.done():
            try:
                await launch_task
            except (asyncio.CancelledError, BrowserStartupError, *_BROWSER_ERRORS) as e:
                logger.debug("in-flight launch of {} unwound during close ({})", browser_id, e)
        await session.close()

    # --- persistence: profiles (Tier A) + manifest (Tier B) -------------------

    def live_browsers(self) -> list[LiveBrowser]:
        """Non-crashed sessions (init + running), by name -- the set that counts toward
        the cap. An ``init`` browser reserves its slot the moment it's registered, so it
        counts here even before Chromium is up.

        Snapshots ``_browsers`` with ``list(...)`` up front so iteration can't
        KeyError if the dict is mutated concurrently (e.g. a close on the loop
        thread popping a name): we sort and filter the snapshot, not the live dict."""
        snapshot = sorted(self._browsers.items())
        return [browser for _, browser in snapshot if not browser._crashed]

    def running_browsers(self) -> list[LiveBrowser]:
        """Only ``running`` sessions, by name -- the set that came up THIS boot with real
        tabs to read. Used by the post-restore reconcile to build fresh entries from
        browsers that actually launched (distinct from the saved-but-not-yet-relaunched
        entries it preserves separately). NOTE: this is no longer the persistence set --
        the durable manifest now snapshots ``live_browsers`` (init + running) so a just-
        created ``init`` browser survives a crash before Chromium is up (finding [5])."""
        snapshot = sorted(self._browsers.items())
        return [browser for _, browser in snapshot if browser._is_running]

    def capacity(self) -> tuple[int, int]:
        """(non-crashed browser count, cap). Counts init + running, mirroring create()'s
        cap check, so the UI gates the 'New browser' button on the same condition
        create() enforces."""
        return len(self.live_browsers()), _MAX_SESSIONS

    async def capacity_async(self) -> tuple[int, int]:
        """``capacity()`` for callers on a Flask worker thread to reach via
        ``bridge.run`` -- running the ``_browsers`` read ON the loop thread (where
        every mutation also happens) is what actually makes it race-free; the
        ``list(...)`` snapshot in ``live_browsers`` is belt-and-suspenders."""
        return self.capacity()

    def _entry_for(self, browser: LiveBrowser) -> fleet_manifest.ManifestEntry:
        """A manifest entry for a live browser: its tab URLs + active tab. Topology
        ONLY -- never ownership/queues (process-scoped) or profile bytes. Uses the
        title-free ``tab_urls()`` so checkpoints don't hammer CDP."""
        urls, active_tab = browser.tab_urls()
        return fleet_manifest.ManifestEntry(id=browser.browser_id, tabs=urls, active_tab=active_tab)

    def _snapshot_manifest_locked(self) -> fleet_manifest.Manifest:
        """Build the durable manifest from the LIVE fleet (init + running). Caller holds
        ``_lock``.

        Init browsers ARE persisted now (finding [5]): a browser the user just created is
        registered ``init`` and its Chromium launch is multi-second, so a daemon crash in
        that window would otherwise lose the browser entirely. Persisting it the moment it
        registers means it is restored next boot (an ``init`` browser has no tabs yet, so
        it restores to the home page -- the same as a fresh create). A persisted entry that
        fails to relaunch is preserved-for-retry by restore's flaked-browser path, not
        stranded; only an explicit ``close`` forgets it. Crashed shells are still excluded
        (they're dead, kept only to report ``crashed`` until the user closes them)."""
        entries = [self._entry_for(browser) for browser in self.live_browsers()]
        return fleet_manifest.Manifest(browsers=entries)

    def _spawn_save(self) -> None:
        """Schedule a manifest checkpoint (fire-and-forget, strong-ref'd). For sync
        callers like the crash hook."""
        async def _do() -> None:
            try:
                await self._save_manifest()
            except (OSError, *_BROWSER_ERRORS) as e:
                logger.debug("crash-triggered manifest checkpoint ignored ({})", e)

        task = asyncio.create_task(_do())
        self._bg_save_tasks.add(task)
        task.add_done_callback(self._bg_save_tasks.discard)

    async def _save_manifest(self) -> None:
        """Checkpoint the manifest if it changed (no-op when nothing did -- idle
        workspaces produce zero backup churn). Snapshots under ``_lock``, writes
        outside it; never called while holding ``_control_lock`` (ownership isn't
        persisted, so there's no lock-ordering hazard)."""
        async with self._lock:
            snapshot = self._snapshot_manifest_locked()
        blob = snapshot.model_dump_json()
        if blob == self._last_manifest_json:
            return
        fleet_manifest.write_manifest(snapshot)
        self._last_manifest_json = blob

    def _scan_profile_names(self) -> list[str]:
        """Browser names that have a persistent profile dir on disk (sorted).

        Only profile suffixes that pass :func:`is_valid_browser_name` are returned.
        That rejects pure-numeric suffixes, so an upgraded workspace's old
        ``browser-use-user-data-dir-0`` / ``-1`` / ``-2`` dirs (from the pre-name
        build) are NOT relaunched as bogus "0"/"1"/"2" named browsers; they fall
        through to the orphan sweep instead."""
        prefix = "browser-use-user-data-dir-"
        names: list[str] = []
        if _PROFILE_ROOT.exists():
            for child in _PROFILE_ROOT.iterdir():
                if not (child.is_dir() and child.name.startswith(prefix)):
                    continue
                suffix = child.name[len(prefix):]
                if is_valid_browser_name(suffix):
                    names.append(suffix)
        return sorted(names)

    def _sweep_orphan_profiles(self, live_names: set[str]) -> None:
        """Delete profile dirs not backing a live browser, to bound Tier-A disk.

        Sweeps both name-valid dirs we no longer want AND legacy numeric dirs from a
        pre-name build (which ``_scan_profile_names`` skips), so an upgrade doesn't
        leave stale numeric profiles around forever."""
        prefix = "browser-use-user-data-dir-"
        if not _PROFILE_ROOT.exists():
            return
        for child in _PROFILE_ROOT.iterdir():
            if not (child.is_dir() and child.name.startswith(prefix)):
                continue
            suffix = child.name[len(prefix):]
            if suffix not in live_names:
                shutil.rmtree(child, ignore_errors=True)

    def forget_profile_dir(self, browser_id: str) -> None:
        """Delete a browser's persistent profile (called on explicit `close`)."""
        shutil.rmtree(_profile_dir(browser_id), ignore_errors=True)

    async def _launch_one_restore(self, name: str, restore_tabs: list[str] | None, active_tab: int) -> bool:
        """Relaunch one saved browser through the SAME register-init -> serialized-launch
        path as ``create``: register it ``init`` under a BRIEF ``_lock`` hold, then await
        its serialized launch (so restore stays eager-sequential -- one Chromium at a
        time). Returns True if it came up ``running``, False if it flaked (the launch
        removed it; left in the manifest for a next-boot retry). Idempotent vs a
        concurrent create that already brought this name up."""
        async with self._lock:
            if name in self._browsers:
                return True  # a concurrent create already brought it up
            live = sum(1 for b in self._browsers.values() if not b._crashed)
            if live >= _MAX_SESSIONS:
                logger.warning("restore hit the fleet cap; deferring browser {}", name)
                return False
            session = self._register_init_locked(name)
        # Await the serialized launch (restore is eager-sequential). persist=False: the
        # post-restore reconcile owns the manifest, so a per-launch save can't race it
        # and drop a flaked-but-wanted browser's preserved entry. On failure ``_launch``
        # removes the browser; we report False so the saved entry is preserved for retry.
        await self._launch(session, restore_tabs=restore_tabs, active_tab=active_tab, persist=False)
        return name in self._browsers and self._browsers[name]._is_running

    async def restore(self) -> None:
        """Bring the fleet back on daemon startup: relaunch saved browsers EAGER-
        SEQUENTIALLY (one at a time -- no cold-boot memory spike; the lock is released
        between launches so read-only routes and ``create`` aren't blocked for the whole
        duration), then reconcile the manifest and sweep TRUE orphan profiles. There is
        NO default browser: a fresh workspace restores to an EMPTY fleet (nothing saved ->
        nothing launched). Read-only routes (ls/state) and ``create`` work during this
        restore -- a create just queues behind the serialized relaunches on ``_lock``.

        Durability rule: a browser that merely flakes on relaunch is NOT forgotten --
        its profile is kept and its manifest entry preserved so it retries next boot.
        Only profiles for names we no longer want are swept.
        """
        saved = fleet_manifest.read_manifest()
        saved_by_name = {e.id: e for e in saved.browsers} if saved is not None else {}
        wanted_names: set[str] = set()

        if saved is not None:
            for entry in sorted(saved.browsers, key=lambda e: e.id):
                wanted_names.add(entry.id)
                await self._launch_one_restore(entry.id, entry.tabs or None, entry.active_tab)
        else:
            # No (current-version) manifest. If name-valid profiles survived on the
            # volume, relaunch them (tabs unknown -> home) rather than wiping the saved
            # logins as a "first boot". Legacy numeric profile dirs are skipped by
            # _scan_profile_names and swept below.
            for profile_name in self._scan_profile_names():
                wanted_names.add(profile_name)
                await self._launch_one_restore(profile_name, None, 0)

        # Reconcile the manifest: fresh snapshots of live browsers + the saved entries
        # for wanted names that FAILED to relaunch (kept so they retry next boot), then
        # sweep only profiles that are neither live nor wanted (true orphans + legacy
        # numeric dirs). A browser created mid-restore is in the live snapshot here (it
        # registered under the same _lock), so it is kept, not dropped.
        await self._reconcile_manifest_after_restore(saved_by_name, wanted_names)

    async def _reconcile_manifest_after_restore(
        self, saved_by_name: dict[str, fleet_manifest.ManifestEntry], wanted_names: set[str]
    ) -> None:
        async with self._lock:
            live_names = {b.browser_id for b in self.running_browsers()}
            entries = [self._entry_for(b) for b in self.running_browsers()]
            # Preserve saved entries for wanted browsers that didn't relaunch this boot.
            for name in sorted(wanted_names - live_names):
                if name in saved_by_name:
                    entries.append(saved_by_name[name])
            entries.sort(key=lambda e: e.id)
            manifest = fleet_manifest.Manifest(browsers=entries)
            # Keep profiles for running + wanted browsers AND any non-crashed browser
            # (e.g. an init created mid-restore whose launch hasn't finished) -- never
            # sweep a profile out from under a browser that's still coming up.
            keep_names = live_names | wanted_names | {b.browser_id for b in self.live_browsers()}
        blob = manifest.model_dump_json()
        if blob != self._last_manifest_json:
            fleet_manifest.write_manifest(manifest)
            self._last_manifest_json = blob
        self._sweep_orphan_profiles(keep_names)

    def start_checkpointing(self) -> None:
        """Begin periodically re-checkpointing the manifest (catches tab-URL drift)."""
        if self._checkpoint_task is None:
            self._checkpoint_task = asyncio.create_task(self._checkpoint_loop())

    async def _checkpoint_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(_MANIFEST_CHECKPOINT_SECONDS)
            try:
                await self._save_manifest()
            except (OSError, *_BROWSER_ERRORS) as e:  # a transient hiccup shouldn't kill the loop
                logger.debug("manifest checkpoint ignored ({})", e)

    async def shutdown(self) -> None:
        self._closed = True
        if self._checkpoint_task is not None:
            self._checkpoint_task.cancel()
        # Final checkpoint so a clean stop captures the latest tabs before teardown.
        try:
            await self._save_manifest()
        except (OSError, *_BROWSER_ERRORS) as e:
            logger.debug("final manifest checkpoint ignored ({})", e)
        for browser_id in list(self._browsers):
            await self.close(browser_id)
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
