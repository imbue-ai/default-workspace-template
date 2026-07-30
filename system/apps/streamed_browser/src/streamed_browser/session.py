"""One streamed-browser session: a private Xvfb display + a full Chromium on it.

Unlike the browser app (headless Chromium + CDP + a rebuilt tab/address-bar UI),
this streams Chromium's OWN interface as pixels: the session owns an Xvfb
display sized to the stream, launches the workspace's Fortress Chromium
maximized onto it, and the video pipe captures the whole display. Input lands
at the display level (see xinput), so native menus, dropdowns and drag work
because they are simply Chromium's.

Lifecycle: created lazily by the service on first viewer connect and kept
alive across viewer reconnects (the profile and page state persist). A dead
Chromium or Xvfb marks the session unhealthy; the next connect replaces it.
"""

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from loguru import logger

_FORTRESS_BINARY = "/opt/fortress/tilion-fortress/tilion"
# The framebuffer is fixed at a cap for the session's life -- Xvfb allocates it
# once and cannot grow it at runtime (verified: RANDR maxes at the initial
# size). Panes resize the WINDOW and the capture region WITHIN this framebuffer
# (see videopipe.set_capture_region + xinput.resize_window), so it must be at
# least as large as the biggest pane we'll honor. 1920x1080 = the H.264 level
# 4.0 ceiling the client decodes; ~8MB of RAM, zero CPU (only the captured
# sub-region is ever encoded).
_FB_W = int(os.environ.get("STREAMED_BROWSER_FB_WIDTH", "1920"))
_FB_H = int(os.environ.get("STREAMED_BROWSER_FB_HEIGHT", "1080"))
# Initial window + capture size, before the viewer's first resize message lands
# (the pane sends its real size on connect, so this is just the cold-start size).
_INIT_W = int(os.environ.get("STREAMED_BROWSER_WIDTH", "1280"))
_INIT_H = int(os.environ.get("STREAMED_BROWSER_HEIGHT", "800"))
_DISPLAY_BASE = 50
_DISPLAY_MAX = 79
_READY_TIMEOUT = 20.0
_PROFILE_DIR = Path("data/.apps/streamed-browser/profile")
# Chromium's managed-policy dirs (brand-independent set; verified on this
# Fortress build by the prior browser-live-view-v2 work, which shipped the
# same write). CommandLineFlagSecurityWarningsEnabled=false suppresses the
# yellow "unsupported command-line flag: --no-sandbox" banner we would
# otherwise stream -- it changes no browser behavior (unlike --test-type,
# which we avoid for stealth). We must pass --no-sandbox because Chromium's
# sandbox needs user namespaces we lack running as root in-container.
_POLICY_DIRS = (
    Path("/etc/chromium/policies/managed"),
    Path("/etc/opt/chrome/policies/managed"),
    Path("/etc/chromium-browser/policies/managed"),
)
_FLAG_WARNING_POLICY = '{"CommandLineFlagSecurityWarningsEnabled": false}\n'


def _suppress_flag_warning_banner() -> None:
    """Write the managed policy that hides the --no-sandbox banner.

    Idempotent and cheap (three tiny writes); best-effort, since the dirs are
    root-owned and a locked-down host may refuse -- the banner is cosmetic, so
    a write failure must never block the session.
    """
    for directory in _POLICY_DIRS:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "minds-flag-warnings.json").write_text(_FLAG_WARNING_POLICY)
        except OSError:
            continue


# PulseAudio: a system-mode daemon and one named null sink whose monitor the
# audio pipe captures. PULSE_SINK routes the browser's output into it, so a
# silent tab produces silence the sample-gate drops. All best-effort: audio is
# strictly additive -- if pulse setup fails the session still streams video.
_PULSE_SERVER = "unix:/var/run/pulse/native"
_PULSE_SINK = "streamed_browser"
AUDIO_SOURCE_DEVICE = f"{_PULSE_SINK}.monitor"


def _ensure_pulse_sink() -> bool:
    """Start the pulse daemon (idempotent) and create the null sink. Returns
    whether the sink is available for capture; never raises.

    Launched with -n (load only our modules) and an anonymous-auth native
    socket at a fixed path: running system-mode pulse as root otherwise leaves
    the socket group-gated, which rejects our own pactl/pcmflux clients with
    "Access denied". The null sink is loaded at launch so it exists before
    Chromium's first audio use.
    """
    env = {**os.environ, "PULSE_SERVER": _PULSE_SERVER}
    try:
        if subprocess.run(["pactl", "info"], env=env, capture_output=True, timeout=5).returncode != 0:
            os.makedirs("/var/run/pulse", exist_ok=True)
            # Foreground daemon as a detached background process (its own
            # session, so it survives this call but supervisord's killasgroup
            # still reaps it on service stop). --daemonize=yes double-forks and
            # trips over a stale PID file in this container; a plain Popen does
            # not. The sink loads at launch via -L, before Chromium's first use.
            subprocess.Popen(
                ["pulseaudio", "--system", "--daemonize=no", "--disallow-exit",
                 "--exit-idle-time=-1", "--log-target=stderr", "-n",
                 "-L", "module-native-protocol-unix auth-anonymous=1 socket=/var/run/pulse/native",
                 "-L", f"module-null-sink sink_name={_PULSE_SINK} rate=48000 channels=2"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if subprocess.run(["pactl", "info"], env=env, capture_output=True, timeout=5).returncode == 0:
                    break
                threading.Event().wait(0.2)
        else:
            # Daemon already up (a prior session): ensure the sink is present.
            subprocess.run(
                ["pactl", "load-module", "module-null-sink", f"sink_name={_PULSE_SINK}",
                 "rate=48000", "channels=2"],
                env=env, capture_output=True, timeout=10,
            )
        check = subprocess.run(
            ["pactl", "list", "short", "sinks"], env=env, capture_output=True, text=True, timeout=5
        )
        return _PULSE_SINK in check.stdout
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("pulse audio setup failed ({}); session will stream video only", error)
        return False


_START_URL = os.environ.get("STREAMED_BROWSER_START_URL", "https://duckduckgo.com")


class SessionStartupError(RuntimeError):
    pass


def is_chromium_installed() -> bool:
    """Fortress installs asynchronously on first boot (env-converge)."""
    return os.access(_FORTRESS_BINARY, os.X_OK)


def _display_is_free(number: int) -> bool:
    return not Path(f"/tmp/.X{number}-lock").exists() and not Path(f"/tmp/.X11-unix/X{number}").exists()


def _x_socket_live(number: int) -> bool:
    path = f"/tmp/.X11-unix/X{number}"
    if not Path(path).exists():
        return False
    probe = socket.socket(socket.AF_UNIX)
    probe.settimeout(1.0)
    try:
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


class StreamedBrowserSession:
    """Owns the Xvfb + Chromium pair for one streamed browser."""

    def __init__(self) -> None:
        self.display: str | None = None
        self.audio_available = False
        self._xvfb: subprocess.Popen[bytes] | None = None
        self._chromium: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    @property
    def is_healthy(self) -> bool:
        with self._lock:
            return (
                self._xvfb is not None
                and self._xvfb.poll() is None
                and self._chromium is not None
                and self._chromium.poll() is None
            )

    def ensure_started(self) -> str:
        """Start (or restart after a crash) and return the display name."""
        with self._lock:
            if (
                self._xvfb is not None
                and self._xvfb.poll() is None
                and self._chromium is not None
                and self._chromium.poll() is None
                and self.display is not None
            ):
                return self.display
            self._stop_locked()
            if shutil.which("Xvfb") is None:
                raise SessionStartupError("Xvfb is not installed in this workspace yet")
            if not is_chromium_installed():
                raise SessionStartupError(
                    "Chromium (Fortress) is still installing in this workspace; try again in a minute"
                )
            number = next((n for n in range(_DISPLAY_BASE, _DISPLAY_MAX + 1) if _display_is_free(n)), None)
            if number is None:
                raise SessionStartupError("no free X display number for the streamed browser")
            display = f":{number}"
            self._xvfb = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", f"{_FB_W}x{_FB_H}x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + _READY_TIMEOUT
            while not _x_socket_live(number):
                if self._xvfb.poll() is not None or time.monotonic() > deadline:
                    self._stop_locked()
                    raise SessionStartupError("Xvfb did not become ready")
                threading.Event().wait(0.1)
            _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            _suppress_flag_warning_banner()
            audio_ok = _ensure_pulse_sink()
            # --no-sandbox: the service runs as root in the workspace container
            # (same constraint as the browser app and the repo's Playwright
            # guidance for runtimes without unprivileged user namespaces).
            self._chromium = subprocess.Popen(
                [
                    _FORTRESS_BINARY,
                    "--no-sandbox",
                    # A/B-measured on the live workspace: SwiftShader GPU-process
                    # compositing burned 0.4-0.7 cores emulating a GPU for a 2D
                    # page; software compositing keeps WebGL (Fortress's
                    # fingerprint surface) while roughly halving Chromium's CPU.
                    "--disable-gpu-compositing",
                    # Wheel scrolling: a ~300ms 60fps animation per click is
                    # unrenderable at the delivered frame rate -- it reads as
                    # lag, floods the encoder, and delays settled (readable)
                    # content. Jump scrolling lands in 1-2 damage frames.
                    "--disable-smooth-scrolling",
                    "--wm-window-animations-disabled",
                    # Sites honoring prefers-reduced-motion drop their own
                    # animations -- less damage to encode for content the
                    # delivered frame rate could not show smoothly anyway.
                    "--force-prefers-reduced-motion",
                    # Bound renderer sprawl on a 2-vCPU host: tab-heavy
                    # browsing otherwise spawns a process per site.
                    "--renderer-process-limit=4",
                    "--no-first-run",
                    "--disable-session-crashed-bubble",
                    "--hide-crash-restore-bubble",
                    "--disable-dev-shm-usage",
                    f"--user-data-dir={_PROFILE_DIR.resolve()}",
                    # Pinned at the origin so capture-region (0,0,w,h) maps
                    # window-pixel -> root-pixel 1:1 (input coords stay correct).
                    "--window-position=0,0",
                    f"--window-size={_INIT_W},{_INIT_H}",
                    _START_URL,
                ],
                env={
                    **os.environ,
                    "DISPLAY": display,
                    "PULSE_SERVER": _PULSE_SERVER,
                    "PULSE_SINK": _PULSE_SINK,
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.display = display
            self.audio_available = audio_ok
            logger.info("streamed browser session up on {} (framebuffer {}x{}, window {}x{})", display, _FB_W, _FB_H, _INIT_W, _INIT_H)
            return display

    def _stop_locked(self) -> None:
        for process in (self._chromium, self._xvfb):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        self._chromium = None
        self._xvfb = None
        self.display = None

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()
