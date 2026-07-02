"""In-mind GitHub authentication recovery: status checks, PAT paste, web/device flow.

Implements the backend half of the in-UI GitHub login modal so that a user
whose GitHub credentials didn't sync into the mind can authenticate `gh` (and
therefore `git push` over HTTPS) without dropping into the ttyd terminal, and
crucially without any agent restart.

Two sign-in paths:

1. Raw personal access token (PAT): `submit_raw_token` feeds the token to
   `gh auth login --with-token` over a REAL stdin pipe. `--with-token` reads
   stdin until EOF, so the token MUST be piped over the child's stdin (via the
   runner's `input=` kwarg) -- a pexpect PTY never sends EOF, so a PTY path
   would hang for the full timeout and never persist the credential. Success
   is asserted by `returncode == 0`, not by parsing output.
2. Web/device flow: `start_web_login` drives `gh auth login --web` via pexpect.
   `gh` prints a `XXXX-XXXX` user code and a `github.com/login/device`
   verification URL; the PTY subprocess is held on the `GitHubAuthService`
   instance between `start_web_login` and `submit_code` so the UI can show the
   code to the user in between. `gh` may print a "Press Enter to open in
   browser" prompt before the code/URL, so a newline is sent first.

After ANY successful login (PAT or web), `gh auth setup-git --hostname <host>`
is run explicitly so the git credential helper is wired. This is what makes
`git push` over HTTPS work with no agent restart -- we do NOT assume
`--with-token` did it. No agent is ever restarted in this module (unlike the
Claude API-key path): `gh` persists into its own credential store, which git
consults per-invocation via the credential helper.

Every `gh` invocation runs with the GitHub token environment variables
(`GH_TOKEN` / `GITHUB_TOKEN` and their enterprise variants) scrubbed from the
child environment. `gh` prioritizes those over its credential store, so an
inherited `GH_TOKEN` (the system_interface process inherits one from the agent
environment) would make `gh auth login` refuse to persist and `gh auth status`
report the env token instead of the store -- the login modal could never write
a durable credential. Scrubbing them for the child forces `gh` to read/write
its store, which is the entire point of the modal. See `_gh_child_env`.

Dependencies that touch the outside world (subprocess invocation and
pexpect-driven PTY spawning) are injected into `GitHubAuthService` at
construction so tests can substitute deterministic fakes without
`unittest.mock` or module-level monkeypatching.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import uuid
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

import pexpect
from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.concurrency_group.subprocess_utils import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_claude.resources.stream_snapshot import strip_ansi

logger = _loguru_logger

_DEFAULT_HOST: Final = "github.com"
_GH_STATUS_TIMEOUT_SECONDS: Final = 10.0
_GH_LOGIN_TIMEOUT_SECONDS: Final = 30.0
_GH_SETUP_GIT_TIMEOUT_SECONDS: Final = 20.0
_GH_WEB_CODE_WAIT_SECONDS: Final = 30.0
_GH_WEB_COMPLETE_WAIT_SECONDS: Final = 60.0

# `gh` gives these environment variables absolute priority over its credential
# store: when any is set, `gh auth login` refuses to persist a new credential
# ("The value of the GH_TOKEN environment variable is being used for
# authentication. To have GitHub CLI store credentials instead, first clear the
# value from the environment.") and `gh auth status` reports the env token
# rather than the store. The system_interface process inherits `GH_TOKEN` from
# the agent environment (see supervisord.conf), so every `gh` invocation in
# this module MUST run with these scrubbed -- otherwise the login modal can
# never write a credential the way its whole design intends (persist into the
# store, wire git via setup-git, no agent restart). We scrub for the child only;
# the parent process environment is untouched.
_GH_TOKEN_ENV_VARS: Final = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)

# Lenient, non-greedy, no trailing punctuation: gh may wrap these in ANSI or
# surrounding prose, so we match just the code/URL shape and nothing more.
_GH_USER_CODE_REGEX = re.compile(r"[A-Z0-9]{4}-[A-Z0-9]{4}")
_GH_VERIFICATION_URL_REGEX = re.compile(r"https://\S*?github\.com/login/device\S*")

# `gh auth status` prints the authenticated login as "Logged in to github.com
# account <name>" (recent gh) or "Logged in to github.com as <name>" (older
# gh); accept both. Non-greedy up to the first whitespace so trailing prose
# (e.g. "(keyring)") is not captured.
_GH_STATUS_LOGGED_IN_REGEX = re.compile(r"Logged in to \S+ (?:account|as) (\S+)")


class GitHubAuthError(RuntimeError):
    """Raised when a GitHub auth flow operation cannot complete."""


# Public type aliases for dependency injection. Tests pass deterministic fakes
# to `GitHubAuthService`; production code uses the module defaults. The
# command runner accepts an optional `input` (stdin) kwarg -- required by the
# PAT path, which pipes the token to `gh auth login --with-token`.
CommandRunner = Callable[..., Any]
PexpectSpawner = Callable[..., Any]


def _gh_child_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the current environment with the GitHub token vars removed.

    `gh` prioritizes `GH_TOKEN` / `GITHUB_TOKEN` (and enterprise variants) over
    its credential store; leaving them set makes `gh auth login` refuse to
    persist and `gh auth status` report the env token instead of the store.
    Since the system_interface process inherits `GH_TOKEN`, every `gh` call in
    this module runs with these scrubbed so gh reads and writes its store. The
    returned dict is a fresh copy; `os.environ` is not mutated.
    """
    child_env = {key: value for key, value in os.environ.items() if key not in _GH_TOKEN_ENV_VARS}
    if overrides is not None:
        child_env.update(overrides)
    return child_env


def _default_command_runner(
    command: list[str],
    timeout: float,
    env: Mapping[str, str] | None = None,
    input: str | None = None,
) -> Any:
    """Run a local command, optionally piping `input` to the child's stdin.

    `run_local_command_modern_version` hardcodes a DEVNULL stdin and exposes
    no `input` parameter, so the PAT path (which must feed the token to
    `gh auth login --with-token` over a real stdin pipe) cannot use it. When
    `input` is given we run the child directly so we can attach a real stdin
    pipe; otherwise we defer to the shared runner so status/setup-git calls go
    through the same code path the rest of the app uses.

    The child always runs with the GitHub token env vars scrubbed (see
    `_gh_child_env`) so `gh` reads and writes its credential store rather than
    deferring to an inherited `GH_TOKEN`.
    """
    if input is not None:
        completed = subprocess.run(
            command,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_gh_child_env(env),
        )
        return FinishedProcess(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=tuple(command),
            is_output_already_logged=False,
        )
    return run_local_command_modern_version(
        command=command, is_checked=False, timeout=timeout, cwd=None, env=_gh_child_env(env)
    )


def _default_pexpect_spawner(executable: str, args: list[str], timeout: float) -> Any:
    return pexpect.spawn(executable, args, timeout=timeout, encoding="utf-8", env=_gh_child_env())


class GitHubAuthStatus(FrozenModel):
    """Parsed output of `gh auth status --hostname <host>`."""

    logged_in: bool = Field(description="Whether gh is authenticated for the host")
    username: str | None = Field(default=None, description="Authenticated GitHub login, if any")
    host: str = Field(default=_DEFAULT_HOST, description="gh host checked")


class GitHubAuthStartResult(FrozenModel):
    """Result of starting the web/device login flow."""

    session_id: str = Field(description="Opaque token for the in-flight gh login session")
    user_code: str = Field(description="Device user code the user types into GitHub")
    verification_url: str = Field(description="URL the user opens to enter the code")


class _GitHubSessionRecord(FrozenModel):
    """Immutable handle for an in-flight web/device login subprocess.

    Pairs with a parallel non-frozen slot that holds the live pexpect process
    object, since that object is not Pydantic-serializable.
    """

    session_id: str
    host: str
    user_code: str
    verification_url: str


def _parse_status_output(raw_output: str, host: str) -> GitHubAuthStatus:
    """Extract logged-in state + username from `gh auth status` output.

    `gh auth status` writes its human-readable report to stderr, styled with
    ANSI. Strip the escapes first, then look for the "Logged in to ..." line.
    """
    cleaned = strip_ansi(raw_output)
    match = _GH_STATUS_LOGGED_IN_REGEX.search(cleaned)
    if match is None:
        return GitHubAuthStatus(logged_in=False, host=host)
    return GitHubAuthStatus(logged_in=True, username=match.group(1), host=host)


def _safe_terminate(process: Any) -> None:
    """Terminate a pexpect spawn without letting teardown errors propagate.

    `pexpect.spawn.isalive()` reaps the child's exit status and wraps
    `ptyprocess` errors in `pexpect.ExceptionPexpect`; `terminate()` can raise
    `OSError` on an already-reaped descriptor. Both live inside the try so a
    half-torn-down process never crashes the caller.
    """
    try:
        if not process.isalive():
            return
        process.terminate(force=True)
    except (OSError, pexpect.ExceptionPexpect) as e:
        logger.warning("gh auth login subprocess terminate raised: {}", e)


def _safe_close(process: Any) -> None:
    """Release the pexpect spawn's PTY file descriptor.

    `pexpect.spawn.close()` can raise `OSError` (e.g. on an already-closed
    descriptor) and `pexpect.ExceptionPexpect` in some teardown paths. Swallow
    and log both since the only thing we can do at this point is drop the
    reference anyway.
    """
    try:
        process.close()
    except (OSError, pexpect.ExceptionPexpect) as e:
        logger.warning("gh auth login subprocess close raised: {}", e)


def _build_status_command(host: str) -> list[str]:
    return ["gh", "auth", "status", "--hostname", host]


def _build_with_token_command(host: str) -> list[str]:
    return ["gh", "auth", "login", "--hostname", host, "--with-token"]


def _build_setup_git_command(host: str) -> list[str]:
    return ["gh", "auth", "setup-git", "--hostname", host]


def _build_web_login_args(host: str) -> list[str]:
    return ["auth", "login", "--hostname", host, "--web", "--git-protocol", "https"]


class GitHubAuthService(MutableModel):
    """Stateful entry point for the in-mind GitHub auth-recovery flows.

    Holds the injected `command_runner` / `pexpect_spawner` dependencies and
    the in-flight web/device login subprocess. One instance is created per
    application and stored on `app.state`; the subprocess held between
    `start_web_login` and `submit_code` rides that instance. Tests construct
    isolated instances with deterministic fakes.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    command_runner: CommandRunner = _default_command_runner
    pexpect_spawner: PexpectSpawner = _default_pexpect_spawner

    # Only one web/device login can be live at a time per instance, which
    # matches the single-mind / single-user deployment model. The lock and the
    # live subprocess are private runtime state, not configuration data.
    _oauth_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _current_record: _GitHubSessionRecord | None = PrivateAttr(default=None)
    _current_process: Any = PrivateAttr(default=None)

    def get_auth_status(self, host: str = _DEFAULT_HOST) -> GitHubAuthStatus:
        """Invoke `gh auth status --hostname <host>` and parse the result.

        Returns `logged_in=False` if the `gh` binary is missing or reports the
        host as unauthenticated, rather than raising: the whole point of the
        modal is to recover from broken auth state, so a logged-out state is a
        normal, expected result -- not an error.
        """
        try:
            result = self.command_runner(_build_status_command(host), _GH_STATUS_TIMEOUT_SECONDS)
        except ProcessSetupError as e:
            logger.warning("gh auth status failed to launch: {}", e)
            return GitHubAuthStatus(logged_in=False, host=host)
        # `gh auth status` exits nonzero when logged out; that is not an error
        # for us. It writes its report to stderr, so parse both streams.
        combined = (result.stdout if isinstance(result.stdout, str) else "") + (
            result.stderr if isinstance(result.stderr, str) else ""
        )
        if result.returncode != 0:
            return GitHubAuthStatus(logged_in=False, host=host)
        return _parse_status_output(combined, host)

    def submit_raw_token(self, token: SecretStr, host: str = _DEFAULT_HOST) -> GitHubAuthStatus:
        """Authenticate `gh` with a pasted PAT, then wire the git credential helper.

        The token is fed to `gh auth login --with-token` over a REAL stdin
        pipe (`input=token + "\\n"`); `--with-token` reads stdin until EOF, so
        this MUST NOT use pexpect (a PTY never sends EOF and would hang the
        full timeout without persisting). Success is asserted by
        `returncode == 0`, never by parsing output.

        After a successful login we ALWAYS run `gh auth setup-git` so git's
        credential helper is wired for HTTPS pushes -- we do not assume
        `--with-token` did it. No agent is restarted.
        """
        login_result = self.command_runner(
            _build_with_token_command(host),
            _GH_LOGIN_TIMEOUT_SECONDS,
            input=token.get_secret_value() + "\n",
        )
        if login_result.returncode != 0:
            stderr = login_result.stderr.strip() if isinstance(login_result.stderr, str) else ""
            raise GitHubAuthError(f"gh auth login --with-token failed (exit {login_result.returncode}): {stderr}")
        self._setup_git(host)
        return self.get_auth_status(host)

    def start_web_login(self, host: str = _DEFAULT_HOST) -> GitHubAuthStartResult:
        """Spawn `gh auth login --web` and return the parsed user code + URL.

        Replaces any prior in-flight session: only one web/device flow can be
        live at a time per instance, matching the single-mind / single-user
        deployment model. The subprocess is held on the instance until
        `submit_code` completes it.
        """
        with self._oauth_lock:
            if self._current_process is not None:
                _safe_terminate(self._current_process)
                _safe_close(self._current_process)
                self._current_record = None
                self._current_process = None
            process, user_code, verification_url = self._spawn_web_and_parse(host)
            record = _GitHubSessionRecord(
                session_id=uuid.uuid4().hex,
                host=host,
                user_code=user_code,
                verification_url=verification_url,
            )
            self._current_record = record
            self._current_process = process
        return GitHubAuthStartResult(
            session_id=record.session_id,
            user_code=record.user_code,
            verification_url=record.verification_url,
        )

    def submit_code(self, session_id: str) -> GitHubAuthStatus:
        """Wait for the web/device login subprocess to complete, then wire git.

        The user has entered the code in their browser; here we block on the
        held subprocess reaching EOF, then ALWAYS run `gh auth setup-git` so
        the credential helper is wired. No agent is restarted.
        """
        with self._oauth_lock:
            record = self._current_record
            process = self._current_process
            if record is None or process is None or record.session_id != session_id:
                raise GitHubAuthError("No active gh login session matches the provided session_id")
            host = record.host
            try:
                _drive_web_completion(process)
            finally:
                # Terminate-then-close runs unconditionally so a timed-out
                # subprocess doesn't outlive the cleared instance-state slot.
                # _safe_terminate is a no-op once the process reached EOF (the
                # success path), so this is safe on both branches.
                _safe_terminate(process)
                _safe_close(process)
                self._current_record = None
                self._current_process = None
        self._setup_git(host)
        return self.get_auth_status(host)

    def abort_login(self) -> None:
        """Drop any in-flight web/device session (e.g. user closed the modal)."""
        with self._oauth_lock:
            if self._current_process is not None:
                _safe_terminate(self._current_process)
                _safe_close(self._current_process)
            self._current_record = None
            self._current_process = None

    def _setup_git(self, host: str) -> None:
        """Run `gh auth setup-git --hostname <host>`; raise on nonzero exit.

        This wires git's credential helper so `git push` over HTTPS uses the
        freshly-stored gh credential with no agent restart. Run after EVERY
        successful login (PAT or web); we do not assume the login command did
        it.
        """
        setup_result = self.command_runner(_build_setup_git_command(host), _GH_SETUP_GIT_TIMEOUT_SECONDS)
        if setup_result.returncode != 0:
            stderr = setup_result.stderr.strip() if isinstance(setup_result.stderr, str) else ""
            raise GitHubAuthError(f"gh auth setup-git failed (exit {setup_result.returncode}): {stderr}")

    def _spawn_web_and_parse(self, host: str) -> tuple[Any, str, str]:
        process = self.pexpect_spawner("gh", _build_web_login_args(host), _GH_WEB_CODE_WAIT_SECONDS)
        # gh may print "Press Enter to open github.com in your browser" before
        # emitting the code/URL. Answer it with a bare newline so the flow
        # proceeds; harmless if no such prompt is shown.
        try:
            process.sendline("")
        except pexpect.ExceptionPexpect as e:
            _safe_terminate(process)
            _safe_close(process)
            raise GitHubAuthError(f"gh auth login subprocess failed sending initial newline: {e}") from e
        match_index = process.expect([_GH_USER_CODE_REGEX, pexpect.EOF, pexpect.TIMEOUT])
        if match_index != 0:
            _safe_terminate(process)
            _safe_close(process)
            if match_index == 1:
                raise GitHubAuthError("gh auth login exited before printing the device code")
            raise GitHubAuthError("Timed out waiting for the device code from gh auth login")
        # The code and URL may be split across gh's output and are ANSI-styled;
        # re-extract both from the full consumed buffer with escapes stripped.
        consumed = strip_ansi((process.before or "") + (process.after or ""))
        code_match = _GH_USER_CODE_REGEX.search(consumed)
        url_match = _GH_VERIFICATION_URL_REGEX.search(consumed)
        if code_match is None or url_match is None:
            _safe_terminate(process)
            _safe_close(process)
            raise GitHubAuthError(
                "Matched the device code in the stream but could not extract the code and verification URL"
            )
        return process, code_match.group(0), url_match.group(0)


def _drive_web_completion(process: Any) -> None:
    """Block on the web/device login subprocess reaching EOF (login complete)."""
    process.timeout = _GH_WEB_COMPLETE_WAIT_SECONDS
    try:
        result = process.expect([pexpect.EOF, pexpect.TIMEOUT])
    except pexpect.ExceptionPexpect as e:
        raise GitHubAuthError(f"gh auth login subprocess failed waiting for completion: {e}") from e
    if result != 0:
        raise GitHubAuthError("Timed out waiting for gh auth login to complete")
