"""One KasmVNC display per browser: the live-view transport.

Each :class:`~browser.session.LiveBrowser` owns a private KasmVNC session -- an X
server that is also the web client streaming it (its HTTP server and websocket
handler are in-process, so there is no second daemon). Chromium is launched
headful into it.

Why per-browser rather than one shared display:

* **Input fidelity is the whole point.** RFB pointer/keyboard events are injected
  as X events at the *display* level, so native right-click context menus, native
  ``<select>`` dropdowns and date pickers, and real click-drag all work -- none of
  which CDP's page-scoped ``Input.dispatch*`` can reach. There is no input code
  here; that is what a VNC server is.
* **Isolation.** One X CLIPBOARD per display, so two browsers can't clobber each
  other's clipboard, and their windows can't overlap on one framebuffer.

CANONICAL LAUNCH. This goes through ``vncserver`` (KasmVNC's own launcher), not
a hand-built ``Xvnc`` command line, and it writes a ``~/.kasmpasswd`` first --
which is what upstream's container startup, LinuxServer's baseimage, and the docs
all do. ``vncserver`` merges the YAML config hierarchy (defaults, system, user,
env, CLI), generates the SSL certificate, and only then spawns ``Xvnc`` with
*computed* arguments. Driving ``Xvnc`` directly means reproducing that by hand,
which is a standing source of "it accepts the socket and then does nothing"
failures. Deviating from the reference deployment is not worth the few processes
it saves.

Two documented flags carry our case:

* ``-fg`` keeps the server in the foreground, so it is an ordinary child of the
  browser service and dies with it (see :meth:`VncDisplay.start`).
* ``-noxstartup`` suppresses the session startup script. The canonical
  single-application pattern is ``-xstartup <script>``, but our application is
  launched by browser-use (it owns the CDP connection and the persistent
  profile), so the display comes up empty and Chromium attaches to it via
  ``DISPLAY``.

There is deliberately NO window manager. Chromium sizes its own window to the
framebuffer (browser-use already pins ``window_size``), and a maximise request
would have nobody to answer it. The visible consequence: X stays in
``PointerRoot`` focus mode, so pointer and keyboard events reach the window under
the cursor but it never receives a ``FocusIn`` -- ``document.hasFocus()`` is
false in the page, so the JS Clipboard API and some autofocus behaviours do not
work. Adding a ~200 KB WM is the fix if that becomes a problem.
"""

import contextlib
import os
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from loguru import logger

# Display numbers start well clear of a workspace's own :0/:99 so a stray shared
# display can never collide with a per-browser one.
_DISPLAY_BASE = int(os.environ.get("BROWSER_VNC_DISPLAY_BASE", "100"))
# The websocket/HTTP port for display :N is _PORT_BASE + (N - _DISPLAY_BASE). Kept
# clear of 8080-8099, which the app scaffolder auto-assigns from by regex-scanning
# supervisord.conf -- it cannot see a port opened at runtime by this module.
_PORT_BASE = int(os.environ.get("BROWSER_VNC_PORT_BASE", "6900"))
# Ceiling on concurrent displays; well above the fleet's session cap.
_MAX_DISPLAYS = 16

# Framebuffer geometry. Matches the window size browser-use pins on the Chromium
# session, so the page fills the framebuffer exactly and frames are never scaled.
# The framebuffer is allocated up front and cannot grow at runtime, so this is the
# hard ceiling on streamed resolution.
_SCREEN_W = int(os.environ.get("BROWSER_VNC_WIDTH", "1280"))
_SCREEN_H = int(os.environ.get("BROWSER_VNC_HEIGHT", "800"))

_READY_TIMEOUT_S = float(os.environ.get("BROWSER_VNC_READY_TIMEOUT", "30"))
_READY_POLL_S = 0.1
_STOP_GRACE_S = 5.0

_VNCSERVER_BINARY = "vncserver"
_KASMVNCPASSWD_BINARY = "kasmvncpasswd"
# Upstream's own default location; vncserver reads it without being told.
_PASSWORD_FILE = Path(os.environ.get("BROWSER_VNC_PASSWORD_FILE", str(Path.home() / ".kasmpasswd")))
_VNC_USER = os.environ.get("BROWSER_VNC_USER", "workspace")

# Each browser's display is registered as its own workspace service, so the
# viewer reaches it at /service/browser-<name>/ -- served at that prefix's ROOT.
# That is what the KasmVNC client needs: it builds absolute /assets/... URLs and
# derives its websocket URL from window.location (path defaults to "websockify"),
# neither of which survives being nested under a second path prefix.
_SERVICE_NAME_PREFIX = "browser-"
# Resolved from this module's location rather than hardcoded, so it is correct
# wherever the repo lives: .../system/apps/browser/src/browser/vnc.py -> parents[4]
# is .../system.
_FORWARD_PORT_SCRIPT = Path(
    os.environ.get(
        "BROWSER_FORWARD_PORT_SCRIPT",
        str(Path(__file__).resolve().parents[4] / "scripts" / "forward_port.py"),
    )
)

# Display numbers handed out in this process. An entry is released on stop().
_allocated: set[int] = set()


class VncStartupError(RuntimeError):
    """The KasmVNC session could not be started (missing binary, or never came up)."""


def service_name_for(browser_id: str) -> str:
    """The workspace service name carrying this browser's live view."""
    return f"{_SERVICE_NAME_PREFIX}{browser_id}"


def is_available() -> bool:
    """Whether KasmVNC is installed yet.

    It lands asynchronously on first container boot via the env-converge one-shot
    (system/scripts/env.d/1010-kasmvnc.sh), so a browser launched in the first
    minute of a fresh workspace may find it absent. Callers gate on the binary
    itself -- the unit's own satisfied condition -- because there are no marker
    files (the env.d contract).
    """
    return shutil.which(_VNCSERVER_BINARY) is not None


def ensure_password_file() -> None:
    """Create ``~/.kasmpasswd`` if absent, with a random password.

    Every reference deployment writes this file -- upstream's ``vnc_startup.sh``
    calls ``kasmvncpasswd`` even for a headless container, and so does
    LinuxServer's baseimage -- and the documentation does not say what happens
    when it is missing. The sessions run with basic auth disabled and
    ``SecurityTypes None`` (the only path in is system_interface's authenticated
    proxy), so the credential is never presented by anyone; the file exists
    because the server expects it to. The password is random rather than fixed so
    that a misconfiguration which *does* start honouring it fails closed.
    """
    if _PASSWORD_FILE.exists():
        return
    _PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(24)
    try:
        subprocess.run(  # noqa: S603 - fixed binary, no shell
            [_KASMVNCPASSWD_BINARY, "-u", _VNC_USER, "-w", "-r", str(_PASSWORD_FILE)],
            input=f"{password}\n{password}\n",
            text=True,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise VncStartupError(f"could not create {_PASSWORD_FILE}: {e}") from e
    with contextlib.suppress(OSError):
        _PASSWORD_FILE.chmod(0o600)
    logger.info("created {} for the browser fleet's VNC sessions", _PASSWORD_FILE)


def _lock_paths(display_num: int) -> tuple[Path, Path]:
    return (Path(f"/tmp/.X{display_num}-lock"), Path(f"/tmp/.X11-unix/X{display_num}"))


def _port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _x_socket_is_live(display_num: int) -> bool:
    """Whether something is actually accepting on this display's X socket.

    Distinguishes a live X server from the leftovers of a dead one: the server
    creates /tmp/.X<n>-lock and /tmp/.X11-unix/X<n> and a SIGKILL leaves BOTH
    behind. Nothing reclaims them and /tmp outlives a service restart, so treating
    mere file existence as "taken" would retire a display number for the life of
    the container.
    """
    _, x_socket = _lock_paths(display_num)
    if not x_socket.exists():
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(str(x_socket))
        except OSError:
            return False
        return True


def _clear_stale_locks(display_num: int) -> None:
    """Remove the lock/socket files of a display whose server is gone.

    Only ever called once :func:`_x_socket_is_live` has said nothing is listening,
    so this cannot unlink a live server's files.
    """
    for path in _lock_paths(display_num):
        with contextlib.suppress(OSError):
            path.unlink()
            logger.debug("cleared stale X lock {}", path)


def _own_display_numbers() -> range:
    return range(_DISPLAY_BASE, _DISPLAY_BASE + _MAX_DISPLAYS)


def _allocate_display() -> int:
    """Pick a free display number, reclaiming any left behind by an unclean exit."""
    for display_num in _own_display_numbers():
        if display_num in _allocated:
            continue
        port = _PORT_BASE + (display_num - _DISPLAY_BASE)
        if _x_socket_is_live(display_num) or _port_is_listening(port):
            continue  # a real server owns this number
        _clear_stale_locks(display_num)
        _allocated.add(display_num)
        return display_num
    raise VncStartupError(f"no free VNC display in :{_DISPLAY_BASE}..:{_DISPLAY_BASE + _MAX_DISPLAYS - 1}")


def reap_orphan_displays() -> int:
    """Kill any KasmVNC session left over from a previous browser-service.

    Process-group reaping (see :meth:`VncDisplay.start`) covers a clean stop or
    restart, but NOT the service being OOM-killed -- earlyoom kills the service
    directly and supervisord never gets to signal the group, so the X servers and
    their Chromium children survive. The service then restarts, restores the fleet
    from the manifest, and brings up a second set beside the first.

    Called at startup BEFORE restore, when by definition this process owns no
    display, so every session in our range is an orphan. Scoped to that range so
    an unrelated X server on the box is never touched. Returns how many were
    killed.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        # No procfs: a non-Linux dev box running the test suite. Nothing to reap,
        # and the workspace this ships to always has one.
        return 0
    killed = 0
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue  # the process exited between listing and reading
        if not argv or not argv[0].endswith(b"Xvnc"):
            continue
        display_arg = next((a.decode() for a in argv[1:] if a.startswith(b":")), None)
        if display_arg is None or not display_arg[1:].isdigit():
            continue
        display_num = int(display_arg[1:])
        if display_num not in _own_display_numbers():
            continue
        pid = int(entry.name)
        logger.warning("reaping orphaned Xvnc :{} (pid {}) left by a previous browser-service", display_num, pid)
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except OSError as e:
            logger.debug("could not signal orphaned Xvnc pid {} ({})", pid, e)
        _clear_stale_locks(display_num)
    return killed


class VncDisplay:
    """A running KasmVNC session for one browser: X framebuffer + HTML5 client."""

    def __init__(self, browser_id: str) -> None:
        self.browser_id = browser_id
        self.display_num = _allocate_display()
        self.port = _PORT_BASE + (self.display_num - _DISPLAY_BASE)
        self.display = f":{self.display_num}"
        self._process: subprocess.Popen[bytes] | None = None

    def _command(self) -> list[str]:
        # Deliberately the vncserver wrapper, not Xvnc -- see the module docstring.
        # Auth off because the only path in is system_interface's authenticated
        # /service/ proxy, which speaks plain HTTP to backends; loopback bind
        # because every other workspace listener binds loopback.
        return [
            _VNCSERVER_BINARY,
            self.display,
            # REQUIRED, and not obviously so: without it vncserver runs its
            # interactive desktop-environment picker (select-de.sh) whenever
            # ~/.vnc/xstartup is absent. With no tty it prints "Please choose
            # Desktop Environment to run" and dies, so the display never comes up
            # and the launch fails on the readiness timeout with nothing in the
            # log explaining why. "manual" is the documented value for "I will
            # start my own applications", which is our case. Passing -select-de
            # also sets the wrapper's assume-yes flag, so it never prompts about
            # overwriting an existing xstartup either.
            "-select-de", "manual",
            "-fg",
            "-noxstartup",
            "-geometry", f"{_SCREEN_W}x{_SCREEN_H}",
            "-depth", "24",
            "-websocketPort", str(self.port),
            "-interface", "127.0.0.1",
            "-sslOnly", "0",
            "-SecurityTypes", "None",
            "-disableBasicAuth",
            "-AlwaysShared",
            # Everything rides the websocket (TCP). KasmVNC also carries an
            # optional WebRTC/UDP transport that we never use: the client's
            # WebRTC toggle is off by default, no UDP port is published, and the
            # tunnel/proxy path couldn't route it anyway. Left alone, though,
            # the server queries a public STUN service for its own IP on every
            # launch ("ICE: Querying public IP...") -- an external call whose
            # answer nothing can ever reach. Pinning publicIP skips the query;
            # the advertised candidate is self-referential and inert.
            "-publicIP", "127.0.0.1",
        ]

    def start(self) -> None:
        """Start the session and block until the display and its web port answer."""
        if not is_available():
            self.release()
            raise VncStartupError(
                f"{_VNCSERVER_BINARY} is not installed yet (env.d/1010-kasmvnc.sh installs it "
                "asynchronously on first boot); retry once the env-converge one-shot has run"
            )
        ensure_password_file()
        logger.info("starting KasmVNC {} for browser {} (port {})", self.display, self.browser_id, self.port)
        # NOT start_new_session: the server stays in browser-service's process
        # group so supervisord's stopasgroup/killasgroup reaps it when the service
        # is stopped or restarted. Detaching would leave the X server (and the
        # Chromium rendering into it) alive after the service died, while restore()
        # brought up a fresh set alongside them.
        # Both streams inherit so the wrapper's own diagnostics land in the
        # service log. Discarding stdout hid the DE-picker failure above behind a
        # bare readiness timeout, which cost far more than the log noise is worth.
        self._process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            self._command(),
            stdout=None,
            stderr=None,
        )
        self._await_ready()
        self._register_service()

    def _await_ready(self) -> None:
        """Wait for a usable display, not merely for the socket file to appear.

        Both conditions matter: the X socket must actually accept a connection (a
        bare ``exists()`` is satisfied by a leftover file from a dead server), and
        the web port must be listening (Chromium can render into a display whose
        client is not yet serving, which would show a blank pane).
        """
        deadline = time.monotonic() + _READY_TIMEOUT_S
        # An Event we never set, purely as the interval timer: wait(timeout) blocks
        # without time.sleep, which this package's ratchet forbids in production
        # code (test_browser_ratchets.py::test_prevent_time_sleep).
        tick = threading.Event()
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                code = self._process.returncode
                self.release()
                raise VncStartupError(f"KasmVNC {self.display} exited during startup (code {code})")
            if _x_socket_is_live(self.display_num) and _port_is_listening(self.port):
                logger.info("KasmVNC {} ready for browser {}", self.display, self.browser_id)
                return
            tick.wait(_READY_POLL_S)
        self.stop()
        raise VncStartupError(f"KasmVNC {self.display} did not become ready within {_READY_TIMEOUT_S}s")

    def _register_service(self) -> None:
        """Publish this display as ``browser-<name>`` so the workspace can route to it.

        Registered only after readiness, so a pane can never be pointed at a port
        that is not yet serving.
        """
        self._run_forward_port(["--name", service_name_for(self.browser_id), "--url", f"http://localhost:{self.port}"])

    def _unregister_service(self) -> None:
        """Drop the service entry so a closed browser leaves no dead route behind."""
        self._run_forward_port(["--remove", "--name", service_name_for(self.browser_id)])

    def _run_forward_port(self, args: list[str]) -> None:
        try:
            subprocess.run(  # noqa: S603 - fixed script path, no shell
                ["python3", str(_FORWARD_PORT_SCRIPT), *args],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (subprocess.SubprocessError, OSError) as e:
            logger.error("forward_port {} failed for browser {} ({})", args, self.browser_id, e)

    def stop(self) -> None:
        """Stop the session, drop its service entry, free its display. Idempotent."""
        self._unregister_service()
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=_STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                logger.warning("KasmVNC {} ignored SIGTERM; killing", self.display)
                with contextlib.suppress(OSError):
                    process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=_STOP_GRACE_S)
        # Unlink unconditionally: a killed server leaves both files, and leaving
        # them would retire this display number permanently.
        _clear_stale_locks(self.display_num)
        self.release()
        logger.info("stopped KasmVNC {} for browser {}", self.display, self.browser_id)

    def release(self) -> None:
        """Return the display number to the pool without touching the process."""
        _allocated.discard(self.display_num)
