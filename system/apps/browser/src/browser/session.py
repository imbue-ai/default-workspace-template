"""Live browser fleet: headful Chromium on a private Xvfb, streamed via pixelflux, with a per-browser ownership state machine.

Each :class:`LiveBrowser` owns one headful Chromium on its OWN private Xvfb display,
launched by ``chrome_launcher`` and observed over the fleet's own ``CdpClient``; the
agent drives it through ``cdp_proxy`` with ``@playwright/cli``.

The fleet owns the browser; it does not own the driving. It launches Chromium, holds the
ownership lease, keeps its own CDP channel for the tab list / foregrounding / crash
detection, and hands the agent a gated CDP endpoint. Everything about *how to click a
button* is the CLI's business. The pixel view is streamed by pixelflux capturing the
display's foreground window as damage-driven H.264 (see videopipe.py / mediastream.py);
human input is XTEST at that display (xinput.py). The fleet's one job on top of the
agent's driving is to foreground the tab it is acting on, so pixelflux shows it -- the
human sees exactly what the agent does and vice versa.

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
import json
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any, Literal

from imbue.imbue_common.mutable_model import MutableModel
from loguru import logger
from pydantic import PrivateAttr

from browser import chrome_launcher
from browser import manifest as fleet_manifest
from browser.cdp_client import CdpClient
from browser.cdp_proxy import BrowserProxy, ProxyServer
from browser.names import first_free_numbered_browser_name, is_valid_browser_name
from browser.oom_retag import notify_chromium_processes_expected

# Errors expected when a target/CDP session goes away underneath us (tab closed,
# navigation, browser killed). The bounded CDP helpers additionally catch broadly
# (best-effort, must never wedge the loop).
_BROWSER_ERRORS = (RuntimeError, ConnectionError, OSError)

ControlOwner = Literal["human", "agent"]

# Explicit per-browser lifecycle. A browser is REGISTERED in the fleet the instant
# create() is called (so the viewer/CLI can address it at once), but its Chromium is
# launched asynchronously and serialized, so it starts in ``init`` and flips to
# ``running`` only once Chromium is up and the active tab is foregrounded. ``crashed`` is
# terminal (Chromium died -- OOM/segfault). Driving/ownership only applies once
# ``running``; the viewer renders deterministically off this field.
Lifecycle = Literal["init", "running", "crashed"]

# Fortress's fixed install path (see the env.d unit
# system/scripts/env.d/1000-playwright-fortress.sh). A stealth, C++-patched
# Chromium fork -- replaces vanilla Chromium as the engine for every browser
# the fleet launches. It installs asynchronously on first container boot via
# the env-converge one-shot; launching a browser before it exists fails, so
# callers gate on the binary itself (the unit's own satisfied condition --
# there are no marker files). The same unit installs Xvfb + xclip for headful
# runs; those gate on the Xvfb binary the same way.
_FORTRESS_EXECUTABLE = "/opt/fortress/tilion-fortress/tilion"

# Vendored, version-pinned unpacked extensions installed beside Fortress by the same
# env.d unit. Each subdirectory holding a manifest.json is passed via --load-extension.
_EXTENSIONS_DIR = "/opt/fortress/extensions"

# The fleet's CDP proxy: ONE websocket server for every browser, addressed by
# ``/<browser-name>/<token>``. Deliberately its own loopback port and deliberately NOT
# mounted on the Flask app -- that app's port is registered with ``forward_port.py`` and
# published to the desktop client, which would expose an unauthenticated CDP endpoint
# outside the workspace. Started once by the runner; None in unit tests that never
# launch a browser.
# Held in a tiny mutable holder rather than rebound as a module global: the repo's
# ratchets forbid the `global` keyword, and an attribute write is the same thing without
# it.
class _ProxyHolder:
    server: "ProxyServer | None" = None


_PROXY = _ProxyHolder()


def set_proxy_server(server: "ProxyServer | None") -> None:
    """Install the process-wide proxy server (called by the runner at startup)."""
    _PROXY.server = server


def proxy_server() -> "ProxyServer | None":
    return _PROXY.server

# Headful on a per-browser virtual display (Xvfb) by default: pixelflux films a real
# X11 window and XTEST injects human input at it, so Chromium must render into a real
# X11 session, not headless. Falls back to headless where no DISPLAY exists (tests,
# bare dev boxes) so those still run. Force either mode with BROWSER_HEADLESS=1/0.
_HEADLESS = os.environ.get("BROWSER_HEADLESS", "0" if os.environ.get("DISPLAY") else "1") != "0"

# --- pixelflux media path --------------------------------------------------------
# Every browser renders headful onto its OWN private Xvfb and is captured/encoded by
# pixelflux (see videopipe.py). This is the ONLY pixel transport -- the human sees
# Chrome's real chrome (tabs + omnibox) as pixels; there is no CDP-screencast path.
# Private display pool, well above the _MAX_SESSIONS cap. Each browser owns one :N.
_DISPLAY_BASE = int(os.environ.get("BROWSER_DISPLAY_BASE", "50"))
_DISPLAY_MAX = int(os.environ.get("BROWSER_DISPLAY_MAX", "69"))
# Per-display framebuffer, fixed at the H.264 level-4 ceiling; the window resizes
# within it (~8MB RAM/display, zero CPU -- only the captured sub-region encodes).
_FB_W = int(os.environ.get("BROWSER_FB_WIDTH", "1920"))
_FB_H = int(os.environ.get("BROWSER_FB_HEIGHT", "1080"))
# Initial Chromium window inside that framebuffer; resized to the pane on connect.
_INIT_WINDOW_W = int(os.environ.get("BROWSER_INIT_WINDOW_W", "1280"))
_INIT_WINDOW_H = int(os.environ.get("BROWSER_INIT_WINDOW_H", "800"))
_XVFB_READY_TIMEOUT = 20.0


def _x_socket_live(number: int) -> bool:
    return Path(f"/tmp/.X11-unix/X{number}").exists()


def _display_is_free(number: int) -> bool:
    return not Path(f"/tmp/.X{number}-lock").exists() and not _x_socket_live(number)


def _spawn_xvfb() -> "tuple[str, subprocess.Popen[bytes]]":
    """Allocate a free display :N from the pool and start an Xvfb on it. Blocking --
    call via a thread. Returns (display, process); raises on failure."""
    if shutil.which("Xvfb") is None:
        raise BrowserStartupError("Xvfb is not installed in this workspace yet")
    number = next((n for n in range(_DISPLAY_BASE, _DISPLAY_MAX + 1) if _display_is_free(n)), None)
    if number is None:
        raise BrowserStartupError("no free X display number for the browser")
    display = f":{number}"
    xvfb = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", f"{_FB_W}x{_FB_H}x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + _XVFB_READY_TIMEOUT
    while not _x_socket_live(number):
        if xvfb.poll() is not None or time.monotonic() > deadline:
            if xvfb.poll() is None:
                xvfb.terminate()
            raise BrowserStartupError("Xvfb did not become ready")
        time.sleep(0.1)
    return display, xvfb


def _stop_xvfb(xvfb: "subprocess.Popen[bytes]") -> None:
    """Terminate a private Xvfb (blocking; call via a thread). Best-effort."""
    if xvfb.poll() is not None:
        return
    xvfb.terminate()
    try:
        xvfb.wait(timeout=5)
    except subprocess.TimeoutExpired:
        xvfb.kill()


# PulseAudio (pcmflux audio path): one SHARED system-mode daemon for the whole fleet,
# plus a PER-BROWSER null sink whose monitor that browser's AudioPipe captures. Each
# browser's Chromium is routed into ITS own sink via PULSE_SINK, so browsers never mix
# audio and a silent tab yields silence the pcmflux sample-gate drops. All best-effort:
# audio is strictly additive -- if pulse setup fails the browser still streams video.
# Uses system mode + anonymous-auth native socket at a
# fixed path: root otherwise leaves the socket group-gated and our clients get
# "Access denied"); the only fleet change is one sink per browser instead of one global.
_PULSE_SERVER = "unix:/var/run/pulse/native"


def _pulse_env() -> dict[str, str]:
    return {**os.environ, "PULSE_SERVER": _PULSE_SERVER}


def _pulse_sink_name(browser_id: str) -> str:
    """A PulseAudio-safe null-sink name giving a browser its own audio bus."""
    safe = "".join(c if c.isalnum() else "_" for c in browser_id)
    return f"browser_{safe}"


def _ensure_pulse_daemon() -> bool:
    """Start the shared system-mode pulse daemon if it isn't already up (idempotent).
    Returns whether it's reachable; never raises."""
    env = _pulse_env()
    try:
        if subprocess.run(["pactl", "info"], env=env, capture_output=True, timeout=5).returncode == 0:
            return True
        os.makedirs("/var/run/pulse", exist_ok=True)
        # Foreground daemon as a detached background process (supervisord's killasgroup
        # still reaps it on service stop). --daemonize=yes double-forks and trips over a
        # stale PID file in this container; a plain Popen does not.
        subprocess.Popen(
            ["pulseaudio", "--system", "--daemonize=no", "--disallow-exit",
             "--exit-idle-time=-1", "--log-target=stderr", "-n",
             "-L", "module-native-protocol-unix auth-anonymous=1 socket=/var/run/pulse/native"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if subprocess.run(["pactl", "info"], env=env, capture_output=True, timeout=5).returncode == 0:
                return True
            time.sleep(0.2)
        return False
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("pulse daemon setup failed ({}); browsers stream video only", error)
        return False


def _ensure_pulse_sink(sink_name: str) -> bool:
    """Ensure the shared daemon is up and a null sink named ``sink_name`` exists (loading
    it only if absent, so a relaunch doesn't stack duplicate sinks). Returns whether the
    sink is available for capture. Blocking -- call via a thread. Never raises."""
    if not _ensure_pulse_daemon():
        return False
    env = _pulse_env()
    try:
        listed = subprocess.run(
            ["pactl", "list", "short", "sinks"], env=env, capture_output=True, text=True, timeout=5
        )
        if sink_name in listed.stdout:
            return True
        subprocess.run(
            ["pactl", "load-module", "module-null-sink", f"sink_name={sink_name}", "rate=48000", "channels=2"],
            env=env, capture_output=True, timeout=10,
        )
        check = subprocess.run(
            ["pactl", "list", "short", "sinks"], env=env, capture_output=True, text=True, timeout=5
        )
        return sink_name in check.stdout
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("pulse sink {} setup failed ({}); streaming video only", sink_name, error)
        return False


def _unload_pulse_sink(sink_name: str) -> None:
    """Unload the null-sink module backing ``sink_name`` so closing a browser doesn't leak
    its sink. Blocking -- call via a thread. Best-effort; never raises."""
    env = _pulse_env()
    try:
        listing = subprocess.run(
            ["pactl", "list", "short", "modules"], env=env, capture_output=True, text=True, timeout=5
        )
        for line in listing.stdout.splitlines():
            if "module-null-sink" in line and f"sink_name={sink_name}" in line:
                index = line.split("\t", 1)[0].strip()
                subprocess.run(["pactl", "unload-module", index], env=env, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        logger.debug("pulse sink {} unload ignored ({})", sink_name, error)


# Page the browser opens on, and the default for "New tab".
# The landing page for a fresh browser, and the fallback when a restore has no saved tabs.
# Carries `hl=en` because Google picks its UI language from IP geolocation, not from
# Accept-Language, and this workspace's egress geolocates to France (an OVH range that is
# physically in Oregon but registered to OVH SAS in Roubaix). Without it the FIRST thing a
# user sees on every new browser is a French consent page. `hl` only means anything to
# Google, so this is not a general fix -- it is the one page where it is worth it, because
# it is the one page we choose.
_HOME_URL = os.environ.get("BROWSER_HOME_URL", "https://www.google.com/?hl=en")

# Server-side cast keepalive: the /cast control socket can sit silent for long
# stretches, so without traffic the system_interface WS proxy closes the idle stream
# (~30s). A periodic ping keeps the backend->client direction alive between control
# messages. (Pixels/audio on /stream have their own heartbeat.)
_KEEPALIVE_SECONDS = 10

# Outbound buffer depth per cast WebSocket. Control messages are produced on the
# loop and drained by a Flask thread; if a client falls behind we drop the OLDEST
# message (only the latest control state matters) rather than block
# the loop. A handful of frames is plenty of slack for a momentarily-slow client.
_CAST_QUEUE_MAX_SIZE = 16

# Each live session = one headful Chromium on its own Xvfb; cap the concurrent count so
# a small compute (e.g. 4 GB) can't be OOM-ed. Override via BROWSER_MAX_SESSIONS.
_MAX_SESSIONS = int(os.environ.get("BROWSER_MAX_SESSIONS", "2"))

_ALLOWED_NAV_SCHEMES = frozenset({"http", "https"})


# Names whose background launch FAILED are remembered briefly so a late/retrying optimistic
# viewer (still in 1013 reconnect-backoff when the launch failed, so it never registered a
# cast queue and missed the launch_failed broadcast) is closed terminally (1008) instead of
# told "try again" forever. A small ring is plenty: the cap is 3, and an entry only needs to
# outlive a viewer's reconnect backoff (a few seconds). See BrowserSessionManager.
_FAILED_LAUNCH_MEMORY = int(os.environ.get("BROWSER_FAILED_LAUNCH_MEMORY", "32"))

# Ownership is a STICKY LEASE: an agent acquires a browser on its first command and holds
# it across subsequent commands. A lease has no live connection to detect a
# dead/wandered-off owner (an ATTACHED-but-silent CDP socket looks identical to an
# abandoned one), so it auto-releases after this many seconds with no
# command (the keepalive loop sweeps it). The human take-control is the instant
# escape hatch; this TTL is the backstop. Env-tunable. Kept at 60s (was 90s): short
# enough that a browser a done agent forgot to release frees up promptly for the human,
# long enough not to yank the lease out from under an agent still thinking between commands.
_LEASE_IDLE_TTL = float(os.environ.get("BROWSER_LEASE_IDLE_TTL", "60"))

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


# Sentinel the fleet wraps its agent-facing nudges in before sending them via
# `mngr message` (see `_message_agent`). These nudges land in the agent's
# transcript as an ordinary user turn; without a marker the system_interface
# transcript UI shows them as a bare user bubble, as if the human had typed
# them. Wrapping lets that UI recognise the message and render it as a collapsed
# system chip instead (like Stop-hook feedback).
#
# CROSS-LAYER CONTRACT: the reading side is the frontend's `BROWSER_FLEET_TAG` in
# system/apps/system_interface/frontend/src/views/message-kinds.ts -- keep the tag in
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
# and restart of a single workspace (lost only on a permanent delete). They are covered
# by the restic host backup, never GitHub sync -- a fat, churny profile has no business
# in a git branch. Override the root for tests / alternate layouts.
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
def _profile_dir(browser_id: str) -> Path:
    """The persistent Chromium ``user_data_dir`` for a browser name.

    The ``browser-use-user-data-dir-`` prefix in the final path component is
    HISTORICAL, and the path must NOT change: browser_use's ``BrowserProfile._copy_profile()``
    (profile.py) treats any other path as a "real" profile to COPY into a throwaway
    temp dir (because the bundled binary is "Google Chrome for Testing", so its
    is_chrome check is True) -- which would silently defeat persistence and recopy
    50-500MB on every launch. A path containing this substring hits its early-return
    and is used in place. Pinned by browser-use==0.13.1 and guarded by an integration
    test; do not rename without updating that test. Only the suffix changed from an
    int to the name string (validated filesystem-safe by names.is_valid_browser_name).
    """
    return _PROFILE_ROOT / f"browser-use-user-data-dir-{browser_id}"


def _is_restorable_url(url: str | None) -> bool:
    """Whether a tab URL is worth persisting/reopening (skip blank and internal pages)."""
    return bool(url) and not url.startswith(("about:", "chrome:", "chrome-error:", "devtools:"))


def deferred_install_ready() -> tuple[bool, str]:
    """Return ``(ready, reason)`` once Chromium is installed."""
    if os.environ.get("BROWSER_SKIP_INSTALL_CHECK") == "1":
        return True, "ready"  # host/CI testing without an installed Fortress
    if not os.access(_FORTRESS_EXECUTABLE, os.X_OK):
        return False, "Chromium is still installing in this workspace; try again in a minute."
    # Headful needs the Xvfb display present; wait for its install too (headless
    # runs -- tests, bare dev boxes -- don't need it).
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
    """One headful Chromium streamed to the user and driven by an agent over gated CDP."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    # The NAME the user/agent sees -- ``browser-<N>`` for daemon-minted browsers
    # ("Browser N" in the UI), a random english pair on ones created by older
    # builds. The addressing key everywhere (CLI arg, cast WS path, manifest id,
    # profile dir). Stable while the browser lives; closing retires the name AND
    # deletes its profile, and only then can a later create mint it again -- so a
    # cached name is the same browser, a 404, or (after an explicit close) a
    # brand-new browser with none of the old one's state. There is no default
    # browser -- every name is created on demand.
    browser_id: str
    controller: ControlOwner = "human"
    owner_agent_id: str | None = None
    owner_agent_name: str | None = None
    human_pinned: bool = False

    # The Chromium process this browser owns, and the fleet's OWN CDP connection to it
    # (tab list, foregrounding, crash detection). The agent never touches either: it
    # talks to ``_proxy``, which holds a separate upstream socket. Three simultaneous CDP
    # clients on one browser were measured to coexist without interference.
    _chrome: "chrome_launcher.ChromeProcess | None" = PrivateAttr(default=None)
    _cdp: CdpClient | None = PrivateAttr(default=None)
    _proxy: BrowserProxy | None = PrivateAttr(default=None)
    # The capability token in the agent's attach URL. A generic CDP client sends no
    # ``x-mngr-agent-id`` header, so the token -- not the header -- is how the proxy tells
    # the lease-holder from any other attacher. Rotated on every ownership write and on
    # every Chromium launch, which makes revocation race-free and makes a client that
    # reconnects across a restart fail cleanly instead of driving a different browser.
    _token: str = PrivateAttr(default="")
    # The agent the current token was minted for. A CDP client sends no identity header, so
    # the token IS the identity: it is issued to one agent, and it stops matching the moment
    # anyone else takes the browser.
    _token_owner: str = PrivateAttr(default="")
    _token_owner_name: str | None = PrivateAttr(default=None)
    # The tab the pane is showing. Was browser-use's ``agent_focus_target_id``; now the
    # fleet's own notion, updated wherever we foreground.
    _active_target_id: str = PrivateAttr(default="")
    # Pixelflux media path: this browser's private Xvfb display (":50"..) and its
    # Xvfb process, allocated in start() and torn down in close().
    _display: str = PrivateAttr(default="")
    _xvfb: "subprocess.Popen[bytes] | None" = PrivateAttr(default=None)
    # pcmflux audio path: this browser's own PulseAudio null sink and the monitor device
    # its AudioPipe captures. Set up in start() (best-effort), torn down in close().
    # ``_audio_source`` is the capture device (the sink's monitor) when audio is available,
    # else "" -- mediastream reads it via the ``audio_capture_device`` property.
    _audio_sink: str = PrivateAttr(default="")
    _audio_source: str = PrivateAttr(default="")
    _input_enabled: asyncio.Event = PrivateAttr(default_factory=_enabled_event)
    # Thread-safe mirror of _input_enabled for the /stream receive thread (which runs
    # OFF the loop and so can't read the asyncio.Event). Written on the loop wherever
    # _input_enabled flips; read lock-free by mediastream before injecting XTEST input.
    # Client-side suppression (only send input while owner==human) is the primary gate;
    # this is the authoritative server backstop for the handoff TOCTOU.
    _input_gate: threading.Event = PrivateAttr(default_factory=threading.Event)
    # Outbound fan-out queues, one per connected cast WebSocket. The WS lives on a
    # Flask thread (thread-per-connection); the loop pushes JSON frames onto its
    # queue and the Flask thread drains and sends them. queue.Queue is thread-safe
    # for the one-producer (loop) / one-consumer (Flask) handoff. The LIST itself is
    # mutated ONLY on the loop thread (register/unregister are awaited via the
    # bridge), so _broadcast can iterate it without a lock -- the single-loop
    # serialization is the guard. Mirrors system/apps/system_interface's WebSocketBroadcaster.
    _cast_queues: list["queue.Queue[str | None]"] = PrivateAttr(default_factory=list)
    _keepalive_task: "asyncio.Task[None] | None" = PrivateAttr(default=None)
    # The in-flight serialized launch task (set by the manager's _spawn_launch). close()
    # awaits it via the manager so a teardown can't race a suspended start() -- the launch
    # finishes/aborts first and observes _closed. None once create's launch isn't pending.
    _launch_task: "asyncio.Task[None] | None" = PrivateAttr(default=None)
    _closed: bool = PrivateAttr(default=False)
    # Serializes direct browser actions + active-tab foregrounding (slow CDP work).
    _lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    # Serializes ALL ownership changes -- the single mutual-exclusion primitive.
    _control_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    _wait_queue: list[_AcquireWaiter] = PrivateAttr(default_factory=list)
    # The sticky-lease activity timestamp the idle-TTL sweep checks. Touched by a
    # FORWARDED CDP frame, never by a merely-open proxy socket -- so a session that is
    # attached but silent still ages out exactly as a silent agent did before.
    _lease_touched_at: float = PrivateAttr(default=0.0)
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
    # active tab is foregrounded, and to ``crashed`` if Chromium dies unexpectedly (OS/OOM
    # kill, segfault) -- detected via the keepalive poll of the fleet's CDP client, or
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
        """Chromium is up -- the only state in which the browser can be driven and the
        viewer shows the live page."""
        return self._lifecycle == "running"

    @property
    def input_allowed(self) -> bool:
        """Thread-safe: may the human's streamed input be injected right now? True iff
        the human holds control (mirror of _input_enabled). Read by mediastream's
        /stream receive thread before each XTEST inject -- the server-side backstop for
        the input/control handoff race."""
        return self._input_gate.is_set()

    @property
    def audio_capture_device(self) -> str | None:
        """The PulseAudio monitor device this browser's AudioPipe should capture, or None
        if audio isn't available for it. Read by mediastream's /stream handler (off-loop);
        set once in start(), so a plain attribute read is thread-safe."""
        return self._audio_source or None

    def _extension_paths(self) -> "tuple[str, ...]":
        """Vendored, version-pinned unpacked extensions to load, if the env.d unit put
        them there. browser-use used to download these from the Chrome Web Store at
        runtime, unpinned, into the browser holding the human's real logins; they are now
        pinned alongside Fortress (see the env.d unit) and simply passed as a flag."""
        root = Path(os.environ.get("BROWSER_EXTENSIONS_DIR", _EXTENSIONS_DIR))
        if not root.is_dir():
            return ()
        return tuple(str(d) for d in sorted(root.iterdir()) if (d / "manifest.json").is_file())

    async def start(self, restore_tabs: list[str] | None = None, active_tab: int = 0) -> None:
        """Launch the headful Chromium and bring up the fleet's own CDP channel.

        Uses a persistent ``user_data_dir`` per browser name so cookies/logins/history
        survive a restart (Chromium's own persistence; we serialize none of it). When
        ``restore_tabs`` is given (a list of URLs from the manifest), reopen those tabs
        in order instead of the single default home page (and re-focus ``active_tab``);
        the persistent profile means they come back logged in.
        """
        self._input_enabled.set()
        self._input_gate.set()  # thread-safe mirror for the /stream input path
        chromium_path = _FORTRESS_EXECUTABLE
        profile_dir = _profile_dir(self.browser_id)
        # Bring up this browser's private Xvfb BEFORE Chromium, then point THIS
        # process's DISPLAY at it right before the launch: the Chromium subprocess
        # inherits this process's env, so DISPLAY must be set on os.environ here. The
        # manager holds _startup_lock across the whole launch, so this process-global
        # write can't race another launch. Xvfb readiness blocks, so spawn off the loop.
        self._display, self._xvfb = await asyncio.to_thread(_spawn_xvfb)
        os.environ["DISPLAY"] = self._display
        logger.info("LiveBrowser {} private display {} up", self.browser_id, self._display)
        # Bring up this browser's own audio sink and route Chromium into it the same way
        # as DISPLAY. Best-effort -- on failure we clear the vars so this browser never
        # inherits a stale sink, and it just streams video.
        self._audio_sink = _pulse_sink_name(self.browser_id)
        audio_ok = await asyncio.to_thread(_ensure_pulse_sink, self._audio_sink)
        if audio_ok:
            os.environ["PULSE_SERVER"] = _PULSE_SERVER
            os.environ["PULSE_SINK"] = self._audio_sink
            self._audio_source = f"{self._audio_sink}.monitor"
        else:
            os.environ.pop("PULSE_SINK", None)
            self._audio_sink = ""
        first_url = next((u for u in (restore_tabs or []) if _is_restorable_url(u)), _HOME_URL)
        try:
            self._chrome = await asyncio.to_thread(
                lambda: chrome_launcher.launch_with_sandbox_retry(
                    no_sandbox=_should_disable_sandbox(),
                    executable=chromium_path,
                    profile_dir=profile_dir,
                    start_url=first_url,
                    window_size=(_INIT_WINDOW_W, _INIT_WINDOW_H),
                    extensions=self._extension_paths(),
                )
            )
        except chrome_launcher.ChromeStartupError as e:
            raise BrowserStartupError(str(e)) from e
        # The Chromium tree just spawned (and its processes self-write their
        # oom_score_adj moments later): have the OOM sweep re-band it.
        notify_chromium_processes_expected()
        if await self._abort_start_if_torn_down():
            return
        self._cdp = CdpClient(self._chrome.http_endpoint)
        try:
            await self._cdp.connect()
        except Exception as e:  # noqa: BLE001  (websockets/JSON errors are outside _BROWSER_ERRORS)
            # Anything here means we have a live Chromium we cannot talk to. Raising the
            # fleet's own startup error is what gets the browser torn down and removed --
            # otherwise it strands in `init`, holding its name and a slot against the cap
            # forever, with Chromium and Xvfb still up and the viewer stuck on "Starting".
            await self._teardown_chrome()
            raise BrowserStartupError(f"could not open a CDP connection to Chromium: {e}") from e
        # A fresh token per launch: a CLI session that reconnects across a service restart
        # is rejected rather than silently driving a different Chromium (§4.1).
        self._mint_token()
        if _PROXY.server is not None:
            self._proxy = BrowserProxy(
                name=self.browser_id,
                upstream_http=self._chrome.http_endpoint,
                public_base=f"ws://127.0.0.1:{_PROXY.server.port}",
                is_allowed=self._token_may_drive,
                on_attach=self._on_proxy_attach,
                on_activity=self._on_proxy_activity,
                page_count=self._page_count,
            )
            _PROXY.server.register(self._proxy)
        await self._open_initial_tabs(restore_tabs, active_tab)
        # Re-check ONE more time right before the terminal flip: a close() or a crash may
        # have landed during any of the awaits above.
        if await self._abort_start_if_torn_down():
            return
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        self._lifecycle = "running"
        self._broadcast(self._control_message())
        logger.info("LiveBrowser {} started (cdp={})", self.browser_id, self._chrome.http_endpoint)

    # --- the proxy's callbacks into ownership --------------------------------

    async def _token_may_drive(self, token: str) -> bool:
        """Whether a frame bearing ``token`` may be forwarded right now.

        The per-frame replacement for ``run_action``'s compare-and-set, and the entire
        human-wins guarantee: the instant a human takes control the token is re-minted, so
        every subsequent frame is refused.

        It also carries what ``run_action`` did on its FIRST call -- acquiring the browser.
        Without that, the URL ``new`` prints could never drive anything: a fresh browser
        rests with the human, and nothing else auto-acquires any more. The sticky lease
        that SKILL.md promises starts here, on the agent's first frame.
        """
        if not token or token != self._token or not self._token_owner:
            return False
        if self._crashed or not self._is_running:
            return False
        if self.controller == "agent" and self.owner_agent_id == self._token_owner and not self.human_pinned:
            self.touch_lease()
            return True
        if self.controller == "human" and not self.human_pinned:
            # Resting browser: take it, exactly as the first direct command used to.
            if await self.acquire(self._token_owner, self._token_owner_name, wait=False) == "acquired":
                self.touch_lease()
                return True
        return False

    async def _on_proxy_attach(self) -> None:
        """Surface the pane on an agent's FIRST attach.

        ``fleet.py``'s ``_action`` used to do this off ``newly_acquired``; that call site
        goes away with the drive verbs, and an agent attaching to a RESTORED browser calls
        neither ``new`` nor ``acquire``, so without this the pane would never come up.
        """
        self._broadcast(self._control_message())

    async def _on_proxy_activity(self, target_id: str | None) -> None:
        """Foreground the target the agent just addressed (debounced by the proxy).

        The whole 'agent acts -> pane follows' behavior.

        Only ever foregrounds a target the agent NAMED. It deliberately does not fall back
        to ``_foreground_active()``: that re-asserts ``_active_target_id``, which is the
        fleet's cache of the last tab *it* foregrounded. A human switching tabs does it
        inside Chrome via XTEST, which never reaches our CDP connection, so that cache goes
        stale the moment they browse -- and re-asserting it yanked the human off the tab
        they had chosen and back onto the agent's old one. If we cannot tell which tab the
        agent is acting on, the right move is to leave the view alone.
        """
        if target_id:
            await self._focus_and_foreground(target_id)
        else:
            logger.debug("pane-follow: frame named no target for {}; leaving the view alone", self.browser_id)

    async def _page_count(self) -> int:
        """Number of real page targets, for the proxy's last-page close guard."""
        if self._cdp is None:
            return 0
        try:
            return len(await self._cdp.page_targets())
        except Exception as e:  # noqa: BLE001  (guard must fail open, not wedge the socket)
            logger.debug("page count for {} failed ({})", self.browser_id, e)
            return 0

    async def _abort_start_if_torn_down(self) -> bool:
        """If close() or a crash landed while ``start`` was suspended at an await, abort the
        launch: kill the Chromium we already brought up (so a close()-during-launch can't
        leak a second handle) and report True so ``start`` returns without flipping to
        ``running``. Returns False (and does nothing) on the normal path."""
        if not (self._closed or self._crashed):
            return False
        await self._teardown_chrome()
        return True

    async def _open_initial_tabs(self, restore_tabs: list[str] | None, active_tab: int = 0) -> None:
        """Open the initial URLs: the saved tabs on restore, else the single home page,
        then foreground the tab that was active before the restart.

        Chromium already came up on the FIRST url (it was passed on the command line), so
        only the remaining ones need targets. Every step is bounded by
        ``_RESTORE_NAV_TIMEOUT`` so one slow/hung URL can't stall startup, and failures are
        swallowed (a tab that won't load just comes up blank -- the profile's cookies are
        already attached either way)."""
        if self._cdp is None:
            return
        urls = [u for u in (restore_tabs or []) if _is_restorable_url(u)] or [_HOME_URL]
        for url in urls[1:]:
            notify_chromium_processes_expected()  # a new renderer self-writes its oom_score_adj
            try:
                await asyncio.wait_for(self._cdp.create_target(url), timeout=_RESTORE_NAV_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                logger.debug("restore new-tab for {} ignored ({})", url, e)
        try:
            targets = await self._cdp.page_targets()
        except Exception as e:  # noqa: BLE001
            logger.debug("restore target list ignored ({})", e)
            return
        if targets:
            index = active_tab if 0 <= active_tab < len(targets) else 0
            await self._focus_and_foreground(targets[index]["targetId"])

    # --- tabs (the fleet's own CDP client is the single source of truth) --------

    def _active_target(self) -> str | None:
        """The tab the pane is showing. Was browser-use's ``agent_focus_target_id``;
        now the fleet's own notion, written wherever we foreground."""
        return self._active_target_id or None

    async def _active_url(self) -> str | None:
        """The current tab's URL (best-effort), for the handoff-request notice."""
        active = self._active_target()
        if not active or self._cdp is None:
            return None
        try:
            targets = await self._cdp.page_targets()
        except Exception as e:  # noqa: BLE001
            logger.debug("active-url targets ignored ({})", e)
            return None
        return next((t["url"] for t in targets if t["targetId"] == active), None)

    async def _focus_and_foreground(self, target_id: str) -> None:
        """The one tab primitive: foreground ``target_id`` in Chrome so pixelflux, which
        films the foreground window, shows it -- and record it as the active tab. Bounded
        so a CDP stall can never wedge the loop; best-effort (a dead/closed target just
        logs)."""
        if self._cdp is None:
            return
        try:
            await asyncio.wait_for(self._cdp.activate(target_id), timeout=5.0)
            self._active_target_id = target_id
        except Exception as e:  # noqa: BLE001  (CDP best-effort; must never wedge the loop)
            logger.debug("focus+foreground {} ignored ({})", target_id, e)

    async def _foreground_active(self) -> None:
        """Foreground the current tab so the pane shows what the agent is on."""
        target = self._active_target()
        if target:
            await self._focus_and_foreground(target)

    async def _tab_list(self) -> list[dict[str, Any]]:
        """The real tabs -- ONE ordering, the one the agent's ``tab-select`` indexes, so
        ``ls`` and the CLI can't disagree. ``page_targets`` applies exactly the filter
        ``playwright-cli tab-list`` applies (real pages only), which is what keeps the two
        views aligned."""
        if self._cdp is None:
            return []
        try:
            targets = await self._cdp.page_targets()
        except Exception as e:  # noqa: BLE001
            logger.debug("tab list ignored ({})", e)
            return []
        active = self._active_target()
        return [
            {"index": i, "title": t.get("title", ""), "url": t.get("url", ""), "active": t["targetId"] == active}
            for i, t in enumerate(targets)
        ]

    async def tab_urls(self) -> tuple[list[str], int]:
        """The restorable tab URLs + the active tab's index, for the manifest. The
        checkpoint runs it every ~10s on the loop, which is fine (a light targets query)."""
        if self._cdp is None:
            return [], 0
        try:
            targets = await self._cdp.page_targets()
        except Exception as e:  # noqa: BLE001
            logger.debug("tab_urls ignored ({})", e)
            return [], 0
        active_target = self._active_target()
        urls: list[str] = []
        active = 0
        for t in targets:
            if _is_restorable_url(t.get("url")):
                if t["targetId"] == active_target:
                    active = len(urls)
                urls.append(t["url"])
        return urls, active

    async def _keepalive_loop(self) -> None:
        """Ping cast (control) sockets periodically so a quiet browser doesn't let the
        WS proxy time out the idle stream; also poll for a crash, sweep idle leases, and
        refresh the viewer's idle-countdown / queue display while an agent holds."""
        dead_polls = 0
        while not self._closed:
            await asyncio.sleep(_KEEPALIVE_SECONDS)
            self._broadcast({"type": "ping"})
            if not self._is_running:
                continue  # init (no ownership yet) or crashed (dead): no sweeps/handoffs
            # Crash detection, and it must NOT depend on anyone being attached: an agent
            # may be between tasks and the human may just be reading, in which case the
            # proxy has no upstream socket at all. The fleet's own CDP client always does,
            # so poll that. Two consecutive misses debounce a transient drop the client is
            # still retrying (``alive`` stays True across a reconnect, exactly as
            # browser-use's ``is_reconnecting`` did).
            if not await self._chrome_alive():
                dead_polls += 1
                if dead_polls >= 2:
                    self._on_disconnected()  # mark crashed + announce (idempotent)
                continue
            dead_polls = 0
            changed = await self._sweep_unclaimed_grant() or await self._sweep_idle_lease()
            # Re-broadcast the full control state every tick regardless of owner (not just
            # while agent-owned): the one-shot control frame for a control change can be lost
            # (a half-open socket, or evicted by the drop-oldest cast queue), which strands a
            # human viewer thinking an agent still holds control -- input suppressed forever
            # even though it's theirs. Re-asserting the live state converges any live-but-
            # desynced client within a tick. setControl is idempotent, so re-sending an
            # unchanged human-owned state is a no-op on the viewer.
            if not changed:
                self._broadcast(self._control_message())

    async def _sweep_idle_lease(self) -> bool:
        """Release a direct-control lease whose owner has gone quiet (dead/wandered-off
        agent). Returns True if it released one.

        An agent that is ATTACHED to the CDP proxy but silent is treated exactly like a
        silent agent was before: the lease ages out. Only a forwarded frame touches
        ``_lease_touched_at``; merely holding the socket open does not.

        Snapshot the control fields (controller / owner_agent_id / _lease_touched_at)
        under ``_control_lock`` so the idle check and the expect-tuple it builds are taken
        from one consistent view -- otherwise a concurrent ownership change between the
        reads could yield a torn expect-tuple. The CAS in ``_transition`` then re-validates
        against the live state before mutating.
        """
        async with self._control_lock:
            controller = self.controller
            owner_agent_id = self.owner_agent_id
            lease_touched_at = self._lease_touched_at
        if controller == "agent" and time.monotonic() - lease_touched_at > _LEASE_IDLE_TTL:
            return await self._transition(to="human", expect=("agent", owner_agent_id, False))
        return False

    async def _sweep_unclaimed_grant(self) -> bool:
        """A resume-queue agent was handed the browser and messaged to resume, but
        hasn't sent a command within ``_CLAIM_WINDOW`` (it was interrupted/killed, or
        never woke). Revoke the grant so the browser passes to the next waiter instead
        of sitting idle for the full idle-TTL on a no-show. ``_granted_at`` is set only
        for a pending grant and cleared the instant the agent's first CDP frame is
        forwarded (``touch_lease``)."""
        async with self._control_lock:
            if (
                self.controller == "agent"
                and self._granted_at
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

        Also rotates the CDP capability token. That is what makes lease revocation
        race-free: the moment control moves, every frame bearing the old token is refused,
        with no socket to hunt down and no window in which a stale attacher can act.
        """
        # Re-mint ONLY when the browser passes to a different agent. Two things must both
        # hold and they pull in opposite directions:
        #
        #  * A human taking control must NOT invalidate the token. The agent's live socket
        #    carries the token it attached with, so re-minting here would kill that socket
        #    permanently -- and after the human hands back the agent could not resume on it.
        #    Re-attaching is not a way out either: a playwright-cli slug is poisoned once its
        #    session is torn down. Handing control back has to leave the agent able to carry
        #    on, which is the whole point of the takeover flow.
        #  * A DIFFERENT agent taking the browser must invalidate it, or the previous holder
        #    could keep driving. That is the rotation that matters.
        #
        # The moment-to-moment guarantee does not depend on rotation at all: `_token_may_drive`
        # refuses every frame while the controller is not the token's own agent. The token is
        # identity, not authority.
        if self._token and to == "agent" and agent_id and agent_id != self._token_owner:
            self._mint_token(agent_id, agent_name)
        self.controller = to
        self.owner_agent_id = agent_id
        self.owner_agent_name = agent_name
        self.human_pinned = pinned
        if to == "human":
            self._input_enabled.set()
            self._input_gate.set()  # thread-safe mirror for the /stream input path
        else:
            self._input_enabled.clear()
            self._input_gate.clear()
            self._lease_touched_at = time.monotonic()  # start the sticky-lease idle clock
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
            # Agents queued (monitor-and-wait) behind the current owner, in FIFO order.
            "waiting": self._waiting_names(),
        }
        # While an agent holds a sticky lease, tell the viewer how long it has been idle
        # and when the idle-TTL will auto-release it, so a watching human knows the
        # browser will free itself.
        if self.controller == "agent" and self._lease_touched_at:
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

    def _spawn_wake(self, agent_id: str, agent_name: str | None) -> None:
        """Schedule a wake, holding a strong ref so the task isn't GC'd before it runs."""
        self._spawn(self._wake_agent(agent_id, agent_name))

    def _on_disconnected(self, _source: object | None = None) -> None:
        """Mark the browser crashed and tell the viewer. Called when the fleet's
        Chromium connection is found dead -- lazily on the agent's next command (see
        run_action) or proactively by the keepalive poll. During our own teardown
        (``_closed``) a dead connection is expected, so it's a no-op then. The agent
        finds out on its next command. Idempotent (guards on ``_crashed``).

        The optional ``_source`` argument is unused; it exists only so tests can invoke
        this the way the old Playwright ``disconnected`` callback did (with a payload)."""
        if self._closed or self._crashed:
            return
        self._crashed = True
        self._spawn(self._announce_crash())

    async def _announce_crash(self) -> None:
        logger.warning("browser {} crashed (Chromium connection lost)", self.browser_id)
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

    async def _chrome_alive(self) -> bool:
        """Whether this browser's Chromium is still up.

        Two independent signals, and both matter. The PROCESS handle catches a kill
        (earlyoom, segfault) even while nothing is attached -- something browser-use could
        only infer from its socket. The CDP ping catches a browser that is alive but no
        longer serving, and gives the client one reconnect attempt so a transient drop is
        not reported as death.
        """
        chrome = self._chrome
        if chrome is None or not chrome.alive:
            return False
        if self._cdp is None:
            return False
        if self._cdp.alive:
            return True
        return await self._cdp.ping()

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

    async def _wake_agent(self, agent_id: str, agent_name: str | None) -> None:
        """Message a queued agent that the browser is its again, so it resumes in a
        fresh turn (it ended its turn when it lost control). If it fails, or the agent
        never shows, the claim window passes the browser on."""
        await self._message_agent(
            agent_id,
            agent_name,
            f"Browser {self.browser_id} was handed back to you (the human finished with it). "
            f"The human may have navigated or resized it, so your earlier element numbers are void: "
            f"re-run `state {self.browser_id}` to re-read the page, then continue where you left off.",
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
            await self._write_control_locked("agent", agent_id, agent_name, pinned=False)
            self._granted_at = time.monotonic()  # start the claim window
            self._spawn_wake(agent_id, agent_name)

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

        Preemption no longer cancels anything in-process: the agent drives over its own
        CDP socket, and losing the lease means its next FRAME is refused by the proxy
        (the socket deliberately stays open -- dropping it poisons the CLI session for
        good). All ``preempt`` still does is queue the displaced agent to resume first.
        """
        async with self._control_lock:
            if expect is not None and self._state_tuple() != expect:
                return False
            if preempt:
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
        # has no Chromium yet (nothing to drive); a crashed one is gone. Both
        # are reported here so task/hold/acquire don't park a waiter on (or try to drive)
        # a browser that can't be driven. run_action gates on lifecycle before it calls
        # acquire, so this is the guard for the task/hold/explicit-acquire paths.
        if self._crashed:
            return "crashed"
        if not self._is_running:
            return "starting"
        async with self._control_lock:
            if self.controller == "agent" and self.owner_agent_id == agent_id:
                self.owner_agent_name = agent_name  # refresh display name on re-acquire
                self._dequeue_resume_locked(agent_id)
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
        return "acquired" if waiter.granted else "busy_human"

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
            active_url = await self._active_url()
            self._broadcast(
                {
                    "type": "handoff_request",
                    "browser_id": self.browser_id,
                    "agent_name": agent_name or agent_id,
                    "reason": reason,
                    "url": active_url,
                    **self._control_state(),
                }
            )
        return True

    async def return_to_agents(self) -> bool:
        """Human hands control back: un-pin (only if currently pinned). Frees any waiter."""
        return await self._transition(to="human", pinned=False, expect=("human", None, True))

    # --- socket bookkeeping ---------------------------------------------------

    async def register_cast_queue(self) -> "queue.Queue[str | None]":
        """Register a new cast WebSocket and SEED its initial sync, atomically on the loop.

        Returns an outbound queue for the Flask cast handler to drain. The control (+ crash)
        sync is pushed BEFORE the queue is added to the fan-out list, so the viewer's first
        message is deterministic. The /cast socket carries ONLY control/ownership now -- no
        pixels (they ride /stream) and no tab list (the viewer shows Chromium's own tab strip
        in the streamed pixels), so nothing tab- or frame-shaped is seeded here.

        The seed is at most two messages onto a fresh, empty queue whose maxsize
        (``_CAST_QUEUE_MAX_SIZE`` = 16) is far larger, so the ``put_nowait``s here can never
        raise ``queue.Full``. Runs on the loop (the runner calls it via ``bridge.run``), so
        the list mutation is single-threaded with respect to :meth:`_broadcast`.
        """
        client_queue: "queue.Queue[str | None]" = queue.Queue(maxsize=_CAST_QUEUE_MAX_SIZE)
        # The control message carries the lifecycle, so the viewer's FIRST message tells
        # it whether to show the init overlay / live page / crashed overlay.
        client_queue.put_nowait(json.dumps(self._control_message(), default=str))
        if self._crashed:  # a viewer opening a crashed browser sees the crash state at once
            client_queue.put_nowait(json.dumps({"type": "crashed", "browser_id": self.browser_id}, default=str))
        # Pixels are seeded on the /stream socket (a fresh SPS/PPS+IDR on connect), not here.
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

    async def _teardown_chrome(self) -> None:
        """Drop the proxy registration, the fleet's CDP client, and Chromium itself.

        Order matters: unregister first so no new agent socket can attach to a browser
        that is going away, then close our own client, then kill the process.
        """
        if self._proxy is not None and _PROXY.server is not None:
            _PROXY.server.unregister(self.browser_id)
        self._proxy = None
        if self._cdp is not None:
            await self._cdp.close()
            self._cdp = None
        chrome = self._chrome
        self._chrome = None
        if chrome is not None:
            await asyncio.to_thread(chrome.kill)

    def _mint_token(self, agent_id: str = "", agent_name: str | None = None) -> str:
        """Fresh capability token, issued to ``agent_id``.

        Minted on every Chromium launch (so a client reconnecting across a service restart
        is rejected rather than silently driving a DIFFERENT Chromium with stale target
        ids) and re-minted whenever the browser changes hands. Rotation is what makes
        revocation race-free: the old token simply stops matching, with no socket to chase.
        """
        self._token = secrets.token_urlsafe(18)
        self._token_owner = agent_id
        self._token_owner_name = agent_name
        return self._token

    @property
    def attach_url(self) -> str:
        """The URL the current token-holder points `playwright-cli attach --cdp=` at."""
        if _PROXY.server is None or not self._token:
            return ""
        return f"http://127.0.0.1:{_PROXY.server.port}/{self.browser_id}/{self._token}"

    async def attach_for(self, agent_id: str, agent_name: str | None) -> dict[str, Any]:
        """Issue ``agent_id`` an attach URL, or say why it can't have one.

        This is where agent-vs-agent exclusion is enforced. The proxy cannot read the
        ``X-Mngr-Agent-Id`` header (a generic CDP client sends none), so identity has to be
        bound here, at the one place that still sees it, and carried by the token.
        """
        if self._crashed:
            return self._crashed_payload()
        if not self._is_running:
            return self._starting_payload()
        async with self._control_lock:
            if self.human_pinned:
                return {"ok": False, "status": "busy_human", **self._control_state()}
            if self.controller == "agent" and self.owner_agent_id not in (None, agent_id):
                return {"ok": False, "status": "busy_agent", **self._control_state()}
            self._mint_token(agent_id, agent_name)
        return {"ok": True, "attach_url": self.attach_url, **self._control_state()}

    def touch_lease(self) -> None:
        """Record agent activity. Called for a FORWARDED frame only (see _sweep_idle_lease)."""
        self._lease_touched_at = time.monotonic()
        self._granted_at = 0.0

    async def close(self) -> None:
        self._closed = True
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
        # Tell every connected viewer this browser is gone BEFORE pushing the shutdown
        # sentinel, so the pane shows the terminal "terminated by an agent" overlay
        # immediately (mirrors the ``crashed`` broadcast) instead of flashing the
        # "Loading browser view…" spinner while its cast socket closes and reconnects.
        # Enqueued ahead of the sentinel, so the ordered per-socket queue sends it first.
        self._broadcast({"type": "closed", "browser_id": self.browser_id})
        # Tell every cast socket to tear down (don't wait for the client to disconnect).
        self._shutdown_cast_queues()
        # Release every queued agent so none hangs on a browser being torn down: wait-queue
        # waiters unblock (their acquire returns `closed`); resume-queue agents are messaged
        # it's gone and cleared.
        async with self._control_lock:
            await self._abandon_queues_locked("closed")
        await self._teardown_chrome()
        # Unload this browser's PulseAudio null sink AFTER Chromium is gone (nothing is
        # routing into it anymore), so closing a browser doesn't leak its sink. The shared
        # daemon stays up for the rest of the fleet. Off the loop; best-effort.
        audio_sink = self._audio_sink
        self._audio_sink = ""
        self._audio_source = ""
        if audio_sink:
            await asyncio.to_thread(_unload_pulse_sink, audio_sink)
        # Tear down this browser's private Xvfb (pixelflux path) AFTER Chromium, so the
        # window is gone first; frees the display back to the pool. Off the loop.
        xvfb = self._xvfb
        self._xvfb = None
        if xvfb is not None:
            await asyncio.to_thread(_stop_xvfb, xvfb)


class BrowserSessionManager(MutableModel):
    """Owns the whole fleet (all live browsers).

    The fleet is shared per workspace: every agent in a mind reaches this one
    manager, so ``ls`` shows one fleet and ownership arbitrates between agents.
    Every browser is created on demand -- there is no default browser and the
    fleet starts EMPTY. A daemon-minted NAME is the first free ``browser-<N>``
    (the canonical form of the UI's "Browser N"), allocated under :attr:`_lock`
    against the live fleet, the manifest, and the on-disk profiles. Closing a
    browser retires its name and deletes its profile, which is what frees the
    slot for a later create without any state carrying over.

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
    # Names explicitly retired via ``close``. A viewer whose tab is still open (or a
    # layout-restored ``?session=<name>`` tab) then resolves to nothing; without this the
    # cast handler can't tell "closed/gone" from "not created yet" and tells the viewer to
    # retry (1013) forever, stuck on "Starting browser...". Consulting this closes it 1008
    # (terminal) instead. Re-creating the name clears it (see _register_init_locked).
    _closed_names: "deque[str]" = PrivateAttr(default_factory=lambda: deque(maxlen=_FAILED_LAUNCH_MEMORY))

    def _register_init_locked(self, name: str) -> LiveBrowser:
        """Construct a LiveBrowser in ``init`` and add it to the registry. Caller must
        hold ``self._lock``, so the cap check + name resolution + insert are atomic (no
        cap overshoot, no duplicate-name TOCTOU). Does NOT launch Chromium -- the caller
        kicks :meth:`_launch` off as a background task after releasing the lock."""
        session = LiveBrowser(browser_id=name)
        session._crash_save_hook = self._spawn_save  # checkpoint promptly if it crashes
        self._browsers[name] = session
        # A fresh registration supersedes any earlier launch-failure OR close for this name
        # (the user re-created it, or restore is retrying it), so it's no longer terminal for
        # a viewer -- drop it from both terminal rings so the cast handler stops 1008-ing it.
        self._clear_failed_launch(name)
        if name in self._closed_names:
            self._closed_names = deque(
                (n for n in self._closed_names if n != name), maxlen=_FAILED_LAUNCH_MEMORY
            )
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

    async def recently_closed_async(self, name: str) -> bool:
        """Whether ``name`` was explicitly closed (and not since re-created). The cast/stream
        handlers use this to terminally close (1008) a viewer of a retired browser instead
        of telling it to retry forever. Read on the loop thread, race-free like the above."""
        return name in self._closed_names

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
        browsers that flaked this boot."""
        async with self._startup_lock:
            if session._closed or session.browser_id not in self._browsers:
                return  # closed (or already removed) while it sat in the launch queue
            try:
                await session.start(restore_tabs=restore_tabs, active_tab=active_tab)
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
        "crashed"). A ``None`` name is minted as the first free ``browser-<N>`` --
        the canonical form of the "Browser N" display name the UI derives from it.
        A provided name is validated (:class:`InvalidBrowserNameError`) and rejected
        on collision (:class:`DuplicateBrowserNameError`). Both paths count the
        manifest's entries and the on-disk profile dirs as taken alongside the live
        registry: a saved browser that has not been restored yet, and a profile a
        crash orphaned between a close and its delete, both hold their name -- so a
        new browser can never adopt an existing profile (and its cookies). Orphaned
        profiles are swept at the next boot, which frees their names.
        """
        async with self._lock:
            # Crashed browsers are dead shells kept only to report "crashed"; they
            # don't count toward the cap, so a crash never blocks opening a new one.
            # init + running both count -- the slot is reserved the moment we register.
            live = sum(1 for browser in self._browsers.values() if not browser._crashed)
            if live >= _MAX_SESSIONS:
                raise FleetFullError(f"{live}/{_MAX_SESSIONS} browsers open -- close one first.")
            persisted_names = self._persisted_names()
            if name is None:
                name = self._fresh_name_locked(persisted_names)
            else:
                if not is_valid_browser_name(name):
                    raise InvalidBrowserNameError(
                        f"'{name}' is not a valid browser name -- use lowercase letters, digits, and "
                        "single dashes (e.g. 'browser-2'), 1-40 characters, no leading/trailing dash."
                    )
                if name in self._browsers:
                    raise DuplicateBrowserNameError(
                        f"the name '{name}' is already in use -- pick another, or close that browser first "
                        "(a crashed browser still holds its name until you close it)."
                    )
                if name in persisted_names:
                    raise DuplicateBrowserNameError(
                        f"the name '{name}' still belongs to a saved browser or its on-disk profile -- "
                        "pick another (a browser that has not been restored yet keeps its name)."
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

    def _persisted_names(self) -> set[str]:
        """Names held by the manifest or an on-disk profile dir, live or not.

        Both are name claims that ``create`` must respect: a manifest entry is a
        saved browser that may not have been restored yet (creates work during
        restore), and a profile dir carries a browser's cookies -- a new browser
        adopting either would be a different object resurrecting the old one's
        identity or state. Cheap synchronous reads (a small JSON file and one
        directory listing), called under ``self._lock`` at create time only.
        """
        saved = fleet_manifest.read_manifest()
        manifest_names = {entry.id for entry in saved.browsers} if saved is not None else set()
        return manifest_names | set(self._scan_profile_names())

    def _fresh_name_locked(self, persisted_names: set[str]) -> str:
        """The first free ``browser-<N>`` name. Caller holds ``self._lock``, so the
        check is against an unchanging ``_browsers`` -- this is the minted-name
        uniqueness guarantee. ``persisted_names`` widens the taken set to saved and
        on-disk claims (see :meth:`_persisted_names`)."""
        return first_free_numbered_browser_name(set(self._browsers) | persisted_names)

    def get(self, browser_id: str) -> LiveBrowser:
        # Dict access raises KeyError for a missing/closed name; callers turn it into a 404.
        return self._browsers[browser_id]

    async def resolve(self, browser_id: str) -> LiveBrowser:
        """:meth:`get` as a coroutine, so the sync web layer can resolve a browser ON the
        loop via ``bridge.run`` -- race-free against a concurrent close popping the name --
        without defining its own ``async def``. There is no default browser: every browser
        is created on demand and addressed by name; a closed/unknown name raises KeyError
        (-> 404) until a later create mints it afresh."""
        return self.get(browser_id)

    def has_browser(self, browser_id: str) -> bool:
        return browser_id in self._browsers

    async def list_browsers(self) -> list[dict[str, Any]]:
        return [await self._browsers[name].describe() for name in sorted(self._browsers)]

    async def close(self, browser_id: str) -> None:
        session = self._browsers.pop(browser_id, None)
        if session is None:
            return
        # Remember it as terminally gone so a still-open viewer tab is closed 1008 rather
        # than looping on 1013 "Starting browser..." (cleared if the name is re-created).
        self._closed_names.append(browser_id)
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

    async def _entry_for(self, browser: LiveBrowser) -> fleet_manifest.ManifestEntry:
        """A manifest entry for a live browser: its tab URLs + active tab. Topology
        ONLY -- never ownership/queues (process-scoped) or profile bytes. Reads
        ``LiveBrowser.tab_urls()`` (async: a light targets query)."""
        urls, active_tab = await browser.tab_urls()
        return fleet_manifest.ManifestEntry(id=browser.browser_id, tabs=urls, active_tab=active_tab)

    async def _snapshot_manifest_locked(self) -> fleet_manifest.Manifest:
        """Build the durable manifest from the LIVE fleet (init + running). Caller holds
        ``_lock``.

        Init browsers ARE persisted now (finding [5]): a browser the user just created is
        registered ``init`` and its Chromium launch is multi-second, so a daemon crash in
        that window would otherwise lose the browser entirely. Persisting it the moment it
        registers means it is restored next boot (an ``init`` browser has no tabs yet, so
        it restores to the home page -- the same as a fresh create). A persisted entry that
        fails to relaunch is preserved-for-retry by restore's flaked-browser path, not
        stranded; only an explicit ``close`` forgets it.

        Crashed (not explicitly-closed) browsers are PRESERVED too, carried forward with
        their last-known entry (we can't query dead Chromium for tabs). Dropping them here
        was silent data loss: a crash excluded the browser from the manifest, so the next
        restart swept its profile -- deleting every login -- contradicting "logins persist
        across restarts". Keeping the entry means its profile survives and it relaunches
        fresh from that profile next boot (logged back in). Only an explicit ``close`` (which
        pops it from ``_browsers`` and forgets its profile) removes it."""
        entries = [await self._entry_for(browser) for browser in self.live_browsers()]
        live_ids = {entry.id for entry in entries}
        crashed_ids = {name for name, browser in self._browsers.items() if browser._crashed and name not in live_ids}
        if crashed_ids and self._last_manifest_json is not None:
            try:
                previous = fleet_manifest.Manifest.model_validate_json(self._last_manifest_json)
            except (ValueError, TypeError) as error:
                logger.debug("could not parse last manifest to preserve crashed entries ({})", error)
            else:
                for entry in previous.browsers:
                    if entry.id in crashed_ids:
                        entries.append(entry)
                        crashed_ids.discard(entry.id)
        # A browser that crashed before its first checkpoint has no prior entry; keep it with
        # empty tabs so its profile (logins) still survives the next restart.
        for name in crashed_ids:
            entries.append(fleet_manifest.ManifestEntry(id=name, tabs=[], active_tab=0))
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
            snapshot = await self._snapshot_manifest_locked()
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
            entries = [await self._entry_for(b) for b in self.running_browsers()]
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
