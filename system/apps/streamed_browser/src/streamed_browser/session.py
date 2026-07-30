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
# Deliberately modest: fewer pixels is the cheapest encode win on a 2-vCPU
# host, and the pane upscales. Bump via env when the host has cores to spare.
_SCREEN_W = int(os.environ.get("STREAMED_BROWSER_WIDTH", "1024"))
_SCREEN_H = int(os.environ.get("STREAMED_BROWSER_HEIGHT", "640"))
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
                ["Xvfb", display, "-screen", "0", f"{_SCREEN_W}x{_SCREEN_H}x24"],
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
                    "--window-position=0,0",
                    f"--window-size={_SCREEN_W},{_SCREEN_H}",
                    "--start-maximized",
                    _START_URL,
                ],
                env={**os.environ, "DISPLAY": display},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.display = display
            logger.info("streamed browser session up on {} ({}x{})", display, _SCREEN_W, _SCREEN_H)
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
