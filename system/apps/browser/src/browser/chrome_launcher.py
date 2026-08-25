"""Spawn and supervise one Fortress (Chromium) process for the fleet.

Replaces browser-use's `BrowserSession.start()`. Deliberately NOT Playwright's
`launch_persistent_context`: that applies Playwright's own default switches, which include
`--enable-automation` (sets `navigator.webdriver = true`, defeating a stealth build) and
`--disable-extensions` (kills the unpacked extensions the fleet loads). See `chrome_args`.

Owning the `Popen` handle directly also gives the fleet two things browser-use's wrapper
did not: an unambiguous process-death signal, and the ability to notice a Chromium orphaned
by a previous service restart before launching a second one onto the same profile (§8.1).
"""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from loguru import logger

from browser import chrome_args

# Chromium writes this into the profile as soon as the debug port is bound. It is also the
# file an agent could read to find the real port -- the proxy is a guardrail, not a boundary.
_DEVTOOLS_PORT_FILE = "DevToolsActivePort"
# Chromium's own lock files, cleared before a relaunch on a persistent profile. DevToolsActivePort
# is included because a stale one names the PREVIOUS run's port, which a launcher that polls for
# it would happily read as this run's (§8.2).
SINGLETON_NAMES = ("SingletonLock", "SingletonSocket", "SingletonCookie", _DEVTOOLS_PORT_FILE)

_PORT_WAIT_S = 30.0
_PORT_POLL_S = 0.1


class ChromeStartupError(RuntimeError):
    """Chromium did not come up, or never published a debug port."""


def clear_stale_singleton(profile_dir: Path) -> None:
    """Remove lock files left by a hard kill so a relaunch is not refused."""
    for name in SINGLETON_NAMES:
        try:
            (profile_dir / name).unlink(missing_ok=True)
        except OSError as e:
            logger.debug("could not clear {} in {} ({})", name, profile_dir, e)


def profile_holder_pid(profile_dir: Path) -> int | None:
    """PID of a LIVE Chromium already using this profile, if any.

    Guards the §8.1 failure: an unexpected `[program:browser]` restart leaves Chromium
    orphaned (supervisord's `stopasgroup` only applies to a deliberate stop), and the restore
    path would then clear the singleton locks the *running* browser holds and launch a second
    Chromium onto the same `user_data_dir` -- two writers, one profile, and the orphan is
    invisible to OOM retagging because it is no longer our descendant.
    """
    marker = f"--user-data-dir={profile_dir}"
    try:
        # `--` terminates pgrep's option parsing. Without it the pattern starts with `--`
        # and pgrep reads it as an (unknown) option, silently matching NOTHING -- which
        # made this guard a no-op that never reaped anything. Caught by running it against
        # a real Fortress, not by any unit test.
        out = subprocess.run(
            ["pgrep", "-f", "--", marker], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("profile-holder probe failed ({})", e)
        return None
    for line in out.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid != os.getpid() and _has_exact_arg(pid, marker):
            return pid
    return None


def _has_exact_arg(pid: int, arg: str) -> bool:
    """Whether ``pid``'s argv contains exactly ``arg`` as one token.

    `pgrep -f` matches a SUBSTRING of the command line, so launching `browser-1` would
    match a healthy, running `browser-10` -- and `reap_orphan` would then SIGKILL it
    mid-session. Names are user-chosen, so this is not hypothetical. Confirm the exact
    argv token before treating anything as an orphan.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False  # no procfs (macOS/dev) or the process is already gone: do not kill
    return arg in raw.decode(errors="replace").split("\0")


def reap_orphan(profile_dir: Path, timeout_s: float = 10.0) -> bool:
    """Kill a Chromium orphaned onto this profile. Returns True if one was reaped."""
    pid = profile_holder_pid(profile_dir)
    if pid is None:
        return False
    logger.warning("reaping orphaned Chromium pid={} on {}", pid, profile_dir)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except OSError as e:
            logger.debug("could not signal orphan {} ({})", pid, e)
            return False
        deadline = time.monotonic() + timeout_s / 2
        while time.monotonic() < deadline:
            if profile_holder_pid(profile_dir) is None:
                return True
            time.sleep(0.2)
    return profile_holder_pid(profile_dir) is None


class ChromeProcess:
    """A launched Fortress process plus its CDP endpoint."""

    def __init__(self, proc: "subprocess.Popen[bytes]", port: int, profile_dir: Path) -> None:
        self.proc = proc
        self.port = port
        self.profile_dir = profile_dir

    @property
    def http_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        if self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        except OSError as e:
            logger.debug("chrome kill ignored ({})", e)


def _read_port(profile_dir: Path) -> int | None:
    """Chromium writes `<port>\\n<ws path>` once the debug socket is bound."""
    try:
        first = (profile_dir / _DEVTOOLS_PORT_FILE).read_text().splitlines()[0].strip()
        return int(first)
    except (OSError, IndexError, ValueError):
        return None


def launch(
    *,
    executable: str,
    profile_dir: Path,
    start_url: str,
    window_size: "tuple[int, int]",
    extensions: "tuple[str, ...]" = (),
    no_sandbox: bool = False,
) -> ChromeProcess:
    """Spawn Chromium and wait for it to publish its debug port.

    DISPLAY / PULSE_* are read from `os.environ` by the child, exactly as before: the
    manager holds its startup lock across the whole launch, so the process-global write
    cannot race another browser coming up.
    """
    if not shutil.which(executable) and not os.access(executable, os.X_OK):
        raise ChromeStartupError(f"Chromium executable not found or not executable: {executable}")
    profile_dir.mkdir(parents=True, exist_ok=True)
    if reap_orphan(profile_dir):
        logger.info("reaped an orphaned Chromium before launching on {}", profile_dir)
    clear_stale_singleton(profile_dir)
    argv = [
        executable,
        *chrome_args.launch_args(
            user_data_dir=str(profile_dir),
            remote_debugging_port=0,
            window_size=window_size,
            extensions=extensions,
            no_sandbox=no_sandbox,
        ),
        start_url,
    ]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + _PORT_WAIT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ChromeStartupError(f"Chromium exited immediately (code {proc.returncode})")
        port = _read_port(profile_dir)
        if port:
            logger.info("Chromium up on debug port {} (profile {})", port, profile_dir.name)
            return ChromeProcess(proc, port, profile_dir)
        time.sleep(_PORT_POLL_S)
    try:
        proc.kill()
    except OSError:
        pass
    raise ChromeStartupError(f"Chromium did not publish a debug port within {_PORT_WAIT_S:.0f}s")


def launch_with_sandbox_retry(*, no_sandbox: bool, **kwargs: object) -> ChromeProcess:
    """Launch, retrying once with the sandbox off if a sandboxed attempt fails.

    Preserves the accommodation the browser-use path had: as root (or with
    BROWSER_NO_SANDBOX) the sandbox is off up front, so the doomed attempt that used to
    turn into a 30s hang never happens; any other runtime that also cannot sandbox is
    covered by the retry.
    """
    try:
        return launch(no_sandbox=no_sandbox, **kwargs)  # type: ignore[arg-type]
    except ChromeStartupError:
        if no_sandbox:
            raise
        logger.warning("sandboxed Chromium launch failed; retrying with --no-sandbox")
        return launch(no_sandbox=True, **kwargs)  # type: ignore[arg-type]
