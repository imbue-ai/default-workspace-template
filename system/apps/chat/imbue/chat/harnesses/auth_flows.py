"""Running one sign-in against one account folder.

A flow is: mint a folder, drive the lane's method into it, and commit an index row if the
harness agrees it worked. The folder is provisional until that row exists, so every failure
path removes it.

Single-flight, deliberately. The PTY machinery this builds on holds one live session at a
time, and a user signing in is doing one thing. `flow_id` is a handle for polling, not a
licence for N concurrent flows -- starting a second flow terminates the first.

Two properties the shapes forced:

* Nothing here advances on its own. The PTY is read when a client polls, so a browser tab
  closed mid-flow would otherwise leave a CLI waiting forever -- codex's device flow polls
  for fifteen minutes. Every flow therefore arms a wall-clock timer that terminates the
  process and removes the folder.
* Success is not scraped. Two of the three PTY lanes print no success line at all, so the
  harness's own probe is what decides. Failure IS scraped, so a rejected code fails in
  seconds rather than waiting out a deadline.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

import pexpect
from loguru import logger as _loguru_logger

from imbue.chat import accounts
from imbue.chat.harnesses.claude.auth import ANTHROPIC_API_KEY_ENV_VAR
from imbue.chat.harnesses.claude.auth import ANTHROPIC_BASE_URL_ENV_VAR
from imbue.chat.harnesses.claude.auth import CredentialPasteError
from imbue.chat.harnesses.claude.auth import MANAGED_AUTH_ENV_KEYS
from imbue.chat.harnesses.claude.auth import SUBSCRIPTION_TOKEN_REFUSAL
from imbue.chat.harnesses.claude.auth import parse_credential_lines
from imbue.chat.harnesses.claude.auth import record_api_key_approval
from imbue.chat.harnesses.codex.sign_in import APP_SERVER_SOCKET_FILENAME
from imbue.chat.harnesses.codex.sign_in import CodexLoginClient
from imbue.chat.harnesses.codex.sign_in import SIGN_IN_FLOW_ENV_VAR
from imbue.chat.harnesses.codex.sign_in import app_server_argv
from imbue.chat.harnesses.codex.sign_in import connect_login_client
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.key_check import CheckedProvider
from imbue.chat.harnesses.key_check import KeyCheck
from imbue.chat.harnesses.key_check import PROVIDER_DISPLAY
from imbue.chat.harnesses.key_check import check_key
from imbue.chat.harnesses.lanes import AppServerMethod
from imbue.chat.harnesses.lanes import CodexLogin
from imbue.chat.harnesses.lanes import EofPolicy
from imbue.chat.harnesses.lanes import LANES
from imbue.chat.harnesses.lanes import Lane
from imbue.chat.harnesses.lanes import PasteMethod
from imbue.chat.harnesses.lanes import PasteSink
from imbue.chat.harnesses.lanes import PtyMethod
from imbue.chat.harnesses.lanes import Scrape
from imbue.chat.harnesses.lanes import SignInMethod
from imbue.chat.harnesses.lanes import Submit
from imbue.chat.harnesses.lanes import get_lane
from imbue.chat.harnesses.lanes import get_method
from imbue.chat.harnesses.pty_auth import PtyAuthError
from imbue.chat.harnesses.pty_auth import drain_pty_stream
from imbue.chat.harnesses.pty_auth import drain_pty_stream_until_quiet
from imbue.chat.harnesses.pty_auth import extract_hyperlink_value
from imbue.chat.harnesses.pty_auth import extract_wrapped_value
from imbue.chat.harnesses.pty_auth import safe_close
from imbue.chat.harnesses.pty_auth import safe_terminate
from imbue.chat.harnesses.pty_auth import spawn_pty
from imbue.chat.harnesses.registry import build_account_binding
from imbue.chat.harnesses.sign_in_relay import BROWSER_ENV_VAR
from imbue.chat.harnesses.sign_in_relay import BROWSER_SHIM_RELATIVE_PATH
from imbue.chat.harnesses.sign_in_relay import CallbackFetcher
from imbue.chat.harnesses.sign_in_relay import RelayCallbackError
from imbue.chat.harnesses.sign_in_relay import RelayTarget
from imbue.chat.harnesses.sign_in_relay import SIGN_IN_URL_FILENAME
from imbue.chat.harnesses.sign_in_relay import SIGN_IN_URL_FILE_ENV_VAR
from imbue.chat.harnesses.sign_in_relay import fetch_loopback_callback
from imbue.chat.harnesses.sign_in_relay import is_relayable_path
from imbue.chat.harnesses.sign_in_relay import parse_relay_target
from imbue.chat.harnesses.sign_in_relay import query_state
from imbue.chat.harnesses.sign_in_relay import read_sign_in_url
from imbue.chat.harnesses.signed_in import SignedIn
from imbue.chat.harnesses.signed_in import is_signed_in
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.app_server_client import LoginCompleted

logger = _loguru_logger

# The CLI's input treats a rapid burst as a paste, so Enter must arrive as its own later
# keystroke or it lands in the field as content.
_CODE_ECHO_QUIET_SECONDS: Final = 0.3
_CODE_ECHO_DEADLINE_SECONDS: Final = 3.0
_READY_WAIT_SECONDS: Final = 20.0
# How long a sign-in's `codex app-server` may take to start listening.
_APP_SERVER_START_SECONDS: Final = 20.0
# Shown beside a saved key the provider could not be asked about.
KEY_UNCHECKED_DETAIL: Final = "Couldn't check this key"
# How long to keep asking whether a submitted code worked. The browser round trip is already
# over by then, so this bounds only the CLI's own exchange with its provider -- long enough
# for a slow network, short enough that a spinner cannot outlive the user's patience.
_VERDICT_DEADLINE_SECONDS: Final = 120.0
# How long the relayed callback waits for the flow to settle once the CLI has it, so the page the
# desktop app shows can say how the sign-in ended. A flow still pending after this is reported as
# finishing, and the chooser shows how it ends.
_RELAYED_VERDICT_WAIT_SECONDS: Final = 20.0
_DEVICE_LOGIN_REFUSED_DETAIL: Final = (
    "ChatGPT couldn't start a sign-in with a code. Turn on device code sign-in for Codex in "
    "ChatGPT's security settings, or use an OpenAI API key instead."
)
# How much of a provider's own reason for a failed sign-in is shown.
_MAX_PROVIDER_REASON_CHARS: Final = 200
# How still a screen has to be before we call it drawn, when the method names no anchor to
# expect. Short enough that a fast CLI is not held up; `settle_s` is the overall budget.
_SETTLE_QUIET_SECONDS: Final = 0.2


class FlowError(PtyAuthError):
    """A sign-in flow could not be started or advanced."""


class FlowState(StrEnum):
    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"


class FlowShape(StrEnum):
    """What the modal has to render, which differs by lane rather than by harness."""

    # Here is a link; approve in the browser and paste the code back.
    URL_THEN_CODE = "url_then_code"
    # Here is a link and a one-time code; type it there and we will wait.
    CODE_THEN_WAIT = "code_then_wait"
    # Paste a key.
    PASTE = "paste"
    # Here is a page to finish in a browser; the sign-in completes by itself. Only a relay from
    # the user's own machine can reach its callback, so without one the chooser offers another way.
    BROWSER = "browser"


class FlowStart(FrozenModel):
    flow_id: str
    shape: FlowShape
    url: str | None = None
    code: str | None = None
    # The sign-in page the minds desktop app can open for this flow and relay the callback of.
    # None when the CLI named none, or one whose callback this workspace cannot serve.
    relay_url: str | None = None


class FlowStatus(FrozenModel):
    state: FlowState
    detail: str | None = None
    account_id: str | None = None


def flow_shape(method: SignInMethod) -> FlowShape:
    if isinstance(method, PasteMethod):
        return FlowShape.PASTE
    if isinstance(method, AppServerMethod):
        return FlowShape.BROWSER if method.login is CodexLogin.BROWSER else FlowShape.CODE_THEN_WAIT
    return FlowShape.CODE_THEN_WAIT if method.submit is Submit.NONE else FlowShape.URL_THEN_CODE


def _extract(raw: str, scrape: Scrape, frame_marker: str | None) -> str | None:
    """Recover a scraped value, preferring an OSC 8 hyperlink target when there is one."""
    strict = re.compile(scrape.strict)
    from_link = extract_hyperlink_value(raw, strict)
    if from_link is not None:
        return from_link
    value = extract_wrapped_value(raw, strict, re.compile(scrape.continuation), frame_marker=frame_marker)
    if value is not None and scrape.min_length is not None and len(value) < scrape.min_length:
        # A short extraction is a wrapped fragment, not the value -- keep draining.
        return None
    return value


class _Session:
    """One live flow. Mutable by design; guarded by the service's lock."""

    flow_id: str
    lane: Lane
    method: SignInMethod
    account_id: str
    # Whether THIS flow created the folder. Only a folder we minted is ours to throw away: a
    # re-auth adopts a committed account, so discarding on failure would delete a live
    # account's credentials and orphan every chat bound to it -- for nothing worse than a
    # mistyped code or an abandoned tab.
    minted: bool
    process: Any
    output: str
    state: FlowState
    detail: str | None
    timer: threading.Timer | None
    # Whether the user's code has been handed to the CLI. Only then is it worth asking the
    # harness whether it worked -- the probe is a network call, and before the browser round
    # trip its answer is a foregone "no".
    code_submitted: bool
    # What the probe last said, or None if it has not run. Only UNKNOWN matters: it means
    # "the check failed", not "the credential is bad", so the folder is worth keeping.
    last_verdict: SignedIn | None
    # Where the browser shim records the sign-in URL; removed with the session.
    scratch_dir: Path | None
    relay_url: str | None
    relay_target: RelayTarget | None
    # The desktop app has handed this flow its callback. It gets exactly one.
    is_callback_relayed: bool
    # A relayed request is out to the CLI. A poll must not settle the flow meanwhile: that
    # tears the CLI down, which could cut off the answer the browser is waiting on.
    is_relay_in_flight: bool
    # An app-server sign-in's connection, and how its login ended once codex says so: the
    # notification, or why waiting for it failed.
    login_client: CodexLoginClient | None
    login_outcome: LoginCompleted | str | None

    def is_value_ready(self, buffer: str) -> bool:
        """Whether the scraped value can be read yet -- the drain loop's stop condition.

        A bound method rather than a closure over the method: the drain loop needs a
        predicate, and this is the one piece of per-flow state it has to see.
        """
        method = self.method
        if not isinstance(method, PtyMethod):
            return True
        return _extract(buffer, method.scrape, method.frame_marker) is not None


def _new_session(lane: Lane, method: SignInMethod, account_id: str, minted: bool) -> _Session:
    session = _Session()
    session.flow_id = uuid.uuid4().hex
    session.lane = lane
    session.method = method
    session.account_id = account_id
    session.minted = minted
    session.process = None
    session.output = ""
    session.state = FlowState.PENDING
    session.detail = None
    session.timer = None
    session.code_submitted = False
    session.last_verdict = None
    session.scratch_dir = None
    session.relay_url = None
    session.relay_target = None
    session.is_callback_relayed = False
    session.is_relay_in_flight = False
    session.login_client = None
    session.login_outcome = None
    return session


class AuthFlowService:
    """Starts, advances, and tears down sign-in flows."""

    _home: Path | None
    _work_dir: Path
    # Restarts the agents bound to an account, by account id. Injected so the flow does not
    # reach into the agent manager, and so a test can watch it without running mngr.
    # Returns nothing: the restart runs on its own thread, because it is serial subprocesses
    # with a 60s timeout each and this service holds one lock across every route.
    _restart_bound_agents: Callable[[str], None]
    _lock: threading.Lock
    _session: _Session | None
    _spawner: Callable[..., Any]
    _probe: Callable[[HarnessType, Path], SignedIn]
    _fetch_callback: CallbackFetcher
    _clock: Callable[[], float]
    _check_key: Callable[[CheckedProvider, str, str | None], KeyCheck]
    _connect_login_client: Callable[[Path], CodexLoginClient]

    @classmethod
    def create(
        cls,
        home: Path | None = None,
        work_dir: Path | None = None,
        spawner: Callable[..., Any] | None = None,
        probe: Callable[[HarnessType, Path], SignedIn] | None = None,
        restart_bound_agents: Callable[[str], None] | None = None,
        fetch_callback: CallbackFetcher | None = None,
        clock: Callable[[], float] | None = None,
        key_checker: Callable[[CheckedProvider, str, str | None], KeyCheck] | None = None,
        login_client_connector: Callable[[Path], CodexLoginClient] | None = None,
    ) -> "AuthFlowService":
        """`spawner` stands in for `spawn_pty`, `probe` for `is_signed_in`.

        All injected rather than patched, matching ClaudeAuthService's `pexpect_spawner`.
        The probe in particular shells out to a real CLI, so a test that does not inject one
        is quietly asserting on whatever this machine happens to have installed; the restart
        shells out to mngr, which a test has even less business doing.
        """
        service = cls()
        service._home = home
        service._work_dir = work_dir or Path("/home/user/workspace")
        service._lock = threading.Lock()
        service._session = None
        service._spawner = spawner or spawn_pty
        service._probe = probe or is_signed_in
        service._restart_bound_agents = restart_bound_agents or (lambda _account_id: None)
        service._fetch_callback = fetch_callback or fetch_loopback_callback
        service._clock = clock or time.monotonic
        service._check_key = key_checker or check_key
        service._connect_login_client = login_client_connector or connect_login_client
        return service

    # lifecycle

    def start(self, lane_id: str, method_id: str, account_id: str | None = None) -> FlowStart:
        """Begin a sign-in. Any flow already running is abandoned.

        `account_id` re-authenticates into an EXISTING folder, so every agent already bound
        to it recovers. Without it a fresh folder is minted.
        """
        lane = get_lane(lane_id)
        method = get_method(lane_id, method_id)

        with self._lock:
            self._drop_locked()
            minted = account_id is None
            if account_id is None:
                account_id, account_path = accounts.mint_account_dir(self._home)
            else:
                # Resolve through the index rather than trusting the caller's string. An id
                # reaching here from a POST body is joined into a path and later removed, and
                # `Path` joins swallow an absolute segment whole ("<root>" / "/etc" -> "/etc")
                # while ".." walks straight out of the accounts root.
                existing = accounts.resolve_account(account_id, self._home)
                # An account's lane is fixed. Without this the requested lane won: a POST
                # naming an anthropic account with `lane_id="openai"` returned a codex device
                # flow and wrote codex's config.toml into the claude account's folder. Every
                # chat bound there then resolved a claude harness against codex credentials.
                if existing.lane != lane.id:
                    raise FlowError(f"that account signs in through {existing.lane}, not {lane.id}")
                account_id = existing.id
                account_path = accounts.account_dir(account_id, self._home)
            binding = build_account_binding(lane.harness)
            binding.seed_account(account_path, self._work_dir)

            # A re-auth leaves the account's credential where it is: the CLI writes the new one
            # over it, and the account keeps working meanwhile. What decides that a sign-in
            # landed is the CLI saying so, never a probe that would also see the old credential.
            session = _new_session(lane, method, account_id, minted)
            self._session = session

            if isinstance(method, PasteMethod):
                # Nothing to drive; the caller supplies the credential on submit. It still
                # gets a deadline: a closed browser tab would otherwise leave the session
                # PENDING and its minted folder on disk until the next sign-in or the next
                # boot, and the service is single-flight, so that session is in the way.
                self._arm_deadline_locked(session, method.flow_deadline_s)
                return FlowStart(flow_id=session.flow_id, shape=FlowShape.PASTE)
            # A missing binary raises pexpect.ExceptionPexpect and a CLI that already exited
            # raises OSError from send(); neither is a FlowError, so without this the session
            # stayed PENDING with no teardown and no deadline. The exception propagates: a
            # FlowError is the CLI having said no, and anything else is a bug.
            is_torn_down = False
            try:
                if isinstance(method, AppServerMethod):
                    url, code = self._drive_app_server_locked(session, method, account_path)
                else:
                    url, code = self._drive_locked(session, method, account_path)
                is_torn_down = True
            except FlowError:
                # `_drive_locked`'s own failure paths already tore the session down.
                is_torn_down = True
                raise
            finally:
                if not is_torn_down:
                    self._teardown_locked(session, keep_folder=not minted)
                    self._session = None
            self._arm_deadline_locked(session, method.flow_deadline_s)
            return FlowStart(
                flow_id=session.flow_id,
                shape=flow_shape(method),
                url=(method.static_url if isinstance(method, PtyMethod) else None) or url,
                code=code,
                relay_url=session.relay_url,
            )

    def _drive_app_server_locked(
        self, session: _Session, method: AppServerMethod, account_path: Path
    ) -> tuple[str | None, str | None]:
        """Start an app-server on the account's codex home and begin its login."""
        session.scratch_dir = Path(tempfile.mkdtemp(prefix="minds-sign-in-"))
        socket_path = session.scratch_dir / APP_SERVER_SOCKET_FILENAME
        env = {
            **os.environ,
            **build_account_binding(session.lane.harness).account_env(account_path),
            BROWSER_ENV_VAR: str(self._work_dir / BROWSER_SHIM_RELATIVE_PATH),
            SIGN_IN_URL_FILE_ENV_VAR: str(session.scratch_dir / SIGN_IN_URL_FILENAME),
            SIGN_IN_FLOW_ENV_VAR: "1",
        }
        session.process = self._spawner(
            _binary_for(session.lane), app_server_argv(socket_path), _APP_SERVER_START_SECONDS, env=env
        )
        # Reading its output while it starts keeps the PTY from filling, and paces the wait.
        session.output = drain_pty_stream(
            session.process,
            session.output,
            lambda _: socket_path.exists(),
            deadline_seconds=_APP_SERVER_START_SECONDS,
        )
        if not socket_path.exists():
            self._fail_locked(session, "Codex did not start its sign-in.")
            raise FlowError(session.detail or "no app-server")
        try:
            client = self._connect_login_client(socket_path)
            session.login_client = client
            if method.login is CodexLogin.BROWSER:
                browser_login = client.start_chatgpt_login()
                login_id, url, code = browser_login.login_id, browser_login.auth_url, None
                relay_target = parse_relay_target(url)
                if relay_target is not None:
                    session.relay_url = url
                    session.relay_target = relay_target
            else:
                device_login = client.start_device_login()
                login_id, url, code = device_login.login_id, device_login.verification_url, device_login.user_code
        except (CodexAppServerError, OSError) as e:
            logger.warning("Codex sign-in could not begin: {}", e)
            # A code sign-in is refused when the ChatGPT account has device codes turned off,
            # which is the default for some accounts; the setting is the user's to change.
            self._fail_locked(
                session,
                _DEVICE_LOGIN_REFUSED_DETAIL
                if method.login is CodexLogin.DEVICE
                else "Codex could not begin the sign-in.",
            )
            raise FlowError(session.detail or "no login") from e
        threading.Thread(
            target=self._await_login,
            args=(session, client, login_id, method.flow_deadline_s),
            name=f"codex-sign-in-{session.flow_id}",
            daemon=True,
        ).start()
        return url, code

    def _await_login(self, session: _Session, client: CodexLoginClient, login_id: str, timeout: float) -> None:
        """Wait for codex to say how the login ended; the next poll settles the flow on it."""
        outcome: LoginCompleted | str
        try:
            outcome = client.wait_login_completed(login_id, timeout)
        except CodexAppServerError as e:
            outcome = str(e)
        with self._lock:
            if session.login_outcome is None:
                session.login_outcome = outcome

    def _drive_locked(self, session: _Session, method: PtyMethod, account_path: Path) -> tuple[str | None, str | None]:
        """Spawn the CLI, get it to the point of showing something, and scrape it."""
        env = {**os.environ, **build_account_binding(session.lane.harness).account_env(account_path)}
        url_file: Path | None = None
        if method.relays_browser_sign_in:
            session.scratch_dir = Path(tempfile.mkdtemp(prefix="minds-sign-in-"))
            url_file = session.scratch_dir / SIGN_IN_URL_FILENAME
            env[BROWSER_ENV_VAR] = str(self._work_dir / BROWSER_SHIM_RELATIVE_PATH)
            env[SIGN_IN_URL_FILE_ENV_VAR] = str(url_file)
        drive_started_at = time.monotonic()
        binary = _binary_for(session.lane)
        session.process = self._spawner(
            binary, list(method.argv), method.scrape_timeout_s, env=env, columns=method.pty_columns
        )

        # A keystroke script is blind without this: a reordered menu would make the same keys
        # choose a different login method, and nothing downstream would notice.
        if method.expect_before_keys is not None:
            if (
                session.process.expect(
                    [re.compile(method.expect_before_keys), pexpect.EOF, pexpect.TIMEOUT],
                    timeout=_READY_WAIT_SECONDS,
                )
                != 0
            ):
                self._fail_locked(session, "The sign-in screen did not appear as expected.")
                raise FlowError(session.detail or "unexpected screen")
            session.output += (session.process.before or "") + (session.process.after or "")
        else:
            # No anchor to expect, so wait for the screen itself to stop changing. Better
            # than a fixed pause: a CLI that draws fast is not made slow, and one that
            # animates forever still gets its full budget.
            session.output = drain_pty_stream_until_quiet(
                session.process, session.output, _SETTLE_QUIET_SECONDS, method.settle_s
            )

        for key in method.keys:
            session.process.send(key)
            # Let the TUI redraw before the next key. A burst reads as a paste, and a menu
            # that has not repainted yet may apply the second key to the previous screen.
            session.output = drain_pty_stream_until_quiet(
                session.process, session.output, method.key_gap_s, method.key_gap_s * 2
            )

        # Only wait on the stream if the trigger is not already in hand. Pacing the
        # keystrokes READS the PTY, so on a CLI that answers immediately the value can
        # already be in `session.output` -- and `expect` cannot match bytes something
        # else has consumed, so waiting on it would time out with the answer in hand.
        if re.search(method.scrape.trigger, session.output) is None:
            # The method's own failure lines are waited on ALONGSIDE the trigger. A CLI that is
            # failing never prints the trigger, so watching only for that means sitting out the
            # whole `scrape_timeout_s` -- thirty seconds of spinner -- and then reporting a
            # timeout, when the CLI said what was wrong in the first second and we were not
            # listening. agy is the case that showed it: a refused sign-in prints
            # "Got an error: ..." and no URL, ever.
            failure_patterns = [re.compile(pattern) for pattern, _ in method.failures]
            index = session.process.expect(
                [re.compile(method.scrape.trigger), *failure_patterns, pexpect.EOF, pexpect.TIMEOUT],
                timeout=method.scrape_timeout_s,
            )
            if 1 <= index <= len(failure_patterns):
                # Report the CLI's own words rather than a timeout it did not have.
                session.output += (session.process.before or "") + (session.process.after or "")
                _, copy = method.failures[index - 1]
                match = re.search(failure_patterns[index - 1], session.output)
                detail = copy.replace("{1}", match.group(1)) if match is not None and match.groups() else copy
                self._fail_locked(session, detail)
                raise FlowError(detail)
            if index != 0:
                self._fail_locked(session, "Timed out waiting for the sign-in details.")
                raise FlowError(session.detail or "no value")
            session.output += (session.process.before or "") + (session.process.after or "")
        # A URL is drained until it can be extracted -- the CLI animates forever afterwards,
        # so there is no quiet gap to wait for.
        session.output = drain_pty_stream(session.process, session.output, session.is_value_ready)
        value = _extract(session.output, method.scrape, method.frame_marker)
        if value is None:
            self._fail_locked(session, "Could not read the sign-in details from the terminal.")
            raise FlowError(session.detail or "extraction failed")
        if url_file is not None:
            # The CLI may print its manual URL before it runs `$BROWSER`, so the shim's file can
            # land a moment after the scrape. Reading the PTY meanwhile keeps its output whole.
            remaining = max(0.0, method.scrape_timeout_s - (time.monotonic() - drive_started_at))
            session.output = drain_pty_stream(
                session.process,
                session.output,
                lambda _: read_sign_in_url(url_file) is not None,
                deadline_seconds=remaining,
            )
            relay_url = read_sign_in_url(url_file)
            relay_target = None if relay_url is None else parse_relay_target(relay_url)
            if relay_target is not None:
                session.relay_url = relay_url
                session.relay_target = relay_target
        return (None, value) if method.static_url else (value, None)

    # advancing

    def submit_code(self, flow_id: str, code: str) -> FlowStatus:
        with self._lock:
            session = self._require_locked(flow_id, must_be_pending=True)
            method = session.method
            if not isinstance(method, PtyMethod) or method.submit is Submit.NONE:
                raise FlowError("this sign-in does not take a code")
            # Two writes: the code, then Enter separately, or the paste heuristic swallows it.
            session.process.send(code)
            session.output = drain_pty_stream_until_quiet(
                session.process, session.output, _CODE_ECHO_QUIET_SECONDS, _CODE_ECHO_DEADLINE_SECONDS
            )
            session.process.send("\r")
            session.code_submitted = True
            # The generous deadline covers the user being away in a browser. Once the code
            # is in, nobody is away any more: either the CLI accepts it in seconds or it
            # never will, and the client is sitting on a spinner the whole time. Swap in the
            # short budget so a flow that silently goes nowhere ends as a visible failure.
            self._arm_deadline_locked(session, _VERDICT_DEADLINE_SECONDS)
            return self._settle_locked(session, method)

    def submit_key(self, flow_id: str, api_key: str, key_provider: str | None = None) -> FlowStatus:
        with self._lock:
            session = self._require_locked(flow_id, must_be_pending=True)
            method = session.method
            if not isinstance(method, PasteMethod):
                raise FlowError("this sign-in does not take a key")
            # The id ends up as a KEY in pi's auth.json, so an unhashable one is a 500 and an
            # unrecognised one silently writes a provider the lane does not have. Checked here
            # rather than at the endpoint so every caller gets the same rule.
            if key_provider is not None:
                known = {k.provider_id for k in session.lane.key_providers}
                if key_provider not in known:
                    raise FlowError(f"{session.lane.provider_name} has no key provider {key_provider!r}")
            # Asked of the provider before anything is written: the harnesses' own probes only
            # see that a key is there, so a mistyped one used to be saved and fail the first turn.
            checked = _key_to_check(method.sink, session.lane, api_key, key_provider)
            key_check = KeyCheck.UNCHECKED if checked is None else self._check_key(*checked)
            if checked is not None and key_check is KeyCheck.REJECTED:
                self._fail_locked(session, f"That key was rejected by {PROVIDER_DISPLAY[checked[0]]}.")
                return FlowStatus(state=FlowState.FAILED, detail=session.detail)
            path = accounts.account_dir(session.account_id, self._home)
            # Write, ask, and put the old credential back if the answer is no. The probe needs
            # the file in place to answer at all, so the write has to happen first -- but on a
            # re-auth the folder is a LIVE account, and leaving a rejected key there would
            # quietly break every agent bound to it until each one's next turn.
            with _credentials_restored_on_error(_credential_paths(method.sink, path)) as before:
                display = _write_paste(method.sink, path, api_key, key_provider, session.lane)
                # Writing the file is not the same as the harness accepting it. Ask before
                # committing, so a key the harness cannot use fails here -- where the user is
                # looking at the field they just typed into -- rather than later, as a chat that
                # silently cannot take a turn.
                verdict = self._probe(session.lane.harness, path)
                session.last_verdict = verdict
                if verdict is SignedIn.NO:
                    _restore_credentials(before)
                    self._fail_locked(session, f"{session.lane.provider_name} did not accept that key.")
                    return FlowStatus(state=FlowState.FAILED, detail=session.detail)
            # UNKNOWN means the check itself could not run (the CLI is missing, the network
            # blinked). That is not evidence against a key the user just pasted, and throwing
            # it away would be the worse mistake.
            status = self._commit_locked(session, display)
            if checked is not None and key_check is KeyCheck.UNCHECKED:
                return status.model_copy_update(to_update(status.field_ref().detail, KEY_UNCHECKED_DETAIL))
            return status

    def adopt_claude_credentials(self, pasted: str) -> accounts.Account:
        """Mint an account from a credential someone else obtained, with no flow involved.

        The Imbue path: the Electron chrome sends what the keys page handed the user. There
        is no terminal to drive and nothing to poll, so this skips the flow machinery and
        goes straight to seed, write, commit -- the account existing IS the signed-in flag.
        """
        managed_env = claude_env_from_paste(pasted)
        lane = get_lane("anthropic")
        with self._lock:
            # Re-key into the account this endpoint already owns rather than minting another.
            # It is called every time the user visits the keys page, and a fresh account per
            # visit leaves a row per re-key -- all but the newest holding a dead credential,
            # and the newest quietly becoming the default for every new chat.
            existing = _adopted_account(lane.id, self._home)
            if existing is not None:
                path = accounts.account_dir(existing.id, self._home)
                write_claude_env(path, managed_env)
                return existing
            account_id, path = accounts.mint_account_dir(self._home)
            build_account_binding(lane.harness).seed_account(path, self._work_dir)
            write_claude_env(path, managed_env)
            return accounts.commit_account(account_id, lane.id, ADOPTED_DISPLAY, self._home)

    def poll(self, flow_id: str) -> FlowStatus:
        with self._lock:
            session = self._require_locked(flow_id)
            if session.state is not FlowState.PENDING:
                return FlowStatus(state=session.state, detail=session.detail, account_id=session.account_id)
            method = session.method
            if isinstance(method, PasteMethod) or session.is_relay_in_flight:
                return FlowStatus(state=FlowState.PENDING)
            if isinstance(method, AppServerMethod):
                return self._settle_app_server_locked(session)
            return self._settle_locked(session, method)

    def abort(self, flow_id: str) -> None:
        with self._lock:
            if self._session is not None and self._session.flow_id == flow_id:
                self._drop_locked()

    def provider_name(self, flow_id: str) -> str:
        """The provider a live flow signs in to, as the chooser names it."""
        with self._lock:
            return self._require_locked(flow_id).lane.provider_name

    def relay_callback(self, flow_id: str, path_and_query: str) -> FlowStatus:
        """Deliver the callback the desktop app received on this flow's loopback port, and say how it went.

        Only the provider's callback is taken -- the path the sign-in URL names and the `state` it
        carries, while the flow is still waiting on it -- and only once. The CLI is asked outside
        the lock, since it answers only after its own token exchange and the polls that report
        that need the lock meanwhile. Then the flow is polled until it settles, briefly, so the
        desktop app can tell the browser the outcome rather than guess it.
        """
        if not is_relayable_path(path_and_query):
            raise FlowError("that is not a callback this sign-in can take")
        with self._lock:
            session = self._require_locked(flow_id, must_be_pending=True)
            target = session.relay_target
            if target is None:
                raise FlowError("this sign-in has no browser callback")
            if session.is_callback_relayed:
                raise FlowError("that sign-in's callback has already been handled")
            if path_and_query.split("?", 1)[0] != target.path or query_state(path_and_query) != target.state:
                raise FlowError("that callback does not belong to this sign-in")
            session.is_callback_relayed = True
            session.is_relay_in_flight = True
        try:
            self._fetch_callback(target.port, path_and_query)
        except RelayCallbackError as e:
            # The CLI could not be reached or took too long; what the flow makes of that is
            # still the answer, so it is asked below like any other.
            logger.warning("Sign-in {}: the CLI did not take the relayed callback: {}", flow_id, e)
        finally:
            with self._lock:
                session.is_relay_in_flight = False
        deadline = self._clock() + _RELAYED_VERDICT_WAIT_SECONDS
        status = self.poll(flow_id)
        # Each poll reads the CLI's output for up to a second, which paces this loop.
        while status.state is FlowState.PENDING and self._clock() < deadline:
            status = self.poll(flow_id)
        return status

    # internals

    def _settle_app_server_locked(self, session: _Session) -> FlowStatus:
        """Decide on what codex said about its login, without waiting for it."""
        session.output = _bounded(
            drain_pty_stream(session.process, session.output, lambda _: False, deadline_seconds=0.2)
        )
        outcome = session.login_outcome
        if outcome is None:
            if session.process is not None and session.process.isalive():
                return FlowStatus(state=FlowState.PENDING)
            self._fail_locked(session, "Codex stopped before the sign-in finished.")
            return FlowStatus(state=FlowState.FAILED, detail=session.detail)
        if isinstance(outcome, LoginCompleted) and outcome.success:
            return self._commit_locked(session, session.lane.provider_name)
        logger.info("Codex sign-in {} did not complete: {}", session.flow_id, outcome)
        reason = outcome.error if isinstance(outcome, LoginCompleted) else None
        self._fail_locked(
            session,
            f"ChatGPT didn't finish the sign-in: {reason.strip()[:_MAX_PROVIDER_REASON_CHARS]}"
            if reason and reason.strip()
            else "The sign-in did not complete.",
        )
        return FlowStatus(state=FlowState.FAILED, detail=session.detail)

    def _settle_locked(self, session: _Session, method: PtyMethod) -> FlowStatus:
        """Read what the CLI has said so far and decide, without blocking on it."""
        session.output = _bounded(
            drain_pty_stream(session.process, session.output, lambda _: False, deadline_seconds=1.0)
        )

        for pattern, copy in method.failures:
            match = re.search(pattern, session.output)
            if match is not None:
                detail = copy.replace("{1}", match.group(1)) if match.groups() else copy
                self._fail_locked(session, detail)
                return FlowStatus(state=FlowState.FAILED, detail=detail)

        alive = bool(session.process is not None and session.process.isalive())
        if method.success is not None:
            # A CLI that announces its sign-in decides it: a clean exit, with its success line. Its
            # probe would also see an old credential, so on a re-auth it could not tell the two apart.
            if alive:
                return FlowStatus(state=FlowState.PENDING)
            if session.process.exitstatus != 0:
                self._fail_locked(session, "The sign-in did not complete.")
                return FlowStatus(state=FlowState.FAILED, detail=session.detail)
            if re.search(method.success, session.output) is not None:
                return self._commit_locked(session, session.lane.provider_name)
            # A clean exit whose wording changed: the probe decides, rather than a reworded line
            # throwing away a sign-in that worked.
            verdict = self._probe(session.lane.harness, accounts.account_dir(session.account_id, self._home))
            session.last_verdict = verdict
            if verdict is SignedIn.YES:
                return self._commit_locked(session, session.lane.provider_name)
            self._fail_locked(session, "The sign-in did not complete.")
            return FlowStatus(state=FlowState.FAILED, detail=session.detail)
        exited_meaning_success = not alive and method.eof_policy is EofPolicy.SUCCESS
        # A CLI that never announces success and never exits leaves the probe as the ONLY
        # thing that can say yes -- so it has to be allowed to run while the CLI is still
        # alive. agy is exactly that: it prints no success line and drops straight into its
        # chat TUI, so gating the probe on the CLI being "done talking" meant a completed
        # sign-in stayed PENDING forever and the flow could never finish.
        if not (exited_meaning_success or not alive or session.code_submitted):
            return FlowStatus(state=FlowState.PENDING)

        # The CLI is done talking.
        path = accounts.account_dir(session.account_id, self._home)

        # Its own probe, not the screen, decides.
        verdict = self._probe(session.lane.harness, path)
        session.last_verdict = verdict
        if verdict is SignedIn.YES:
            return self._commit_locked(session, session.lane.provider_name)
        if verdict is SignedIn.UNKNOWN:
            # Keep the folder: a network blink is not evidence the sign-in failed, and the
            # user may have just finished a browser round trip we would be throwing away.
            return FlowStatus(state=FlowState.PENDING)
        # The CLI is gone and its own probe says no. Whatever the method's EOF policy means for
        # a clean exit, there is nothing left that could still turn this into a success.
        #
        # Without this, a SUCCESS-policy method that exits non-zero -- codex's device auth when
        # the user denies the request or lets the code expire -- fell through to PENDING and sat
        # there for the full 900-second deadline, polling every two seconds and spawning a
        # `codex login status` subprocess each time, roughly 450 of them, before finally saying
        # it timed out. It knew within a second.
        if not alive:
            self._fail_locked(session, "The sign-in did not complete.")
            return FlowStatus(state=FlowState.FAILED, detail=session.detail)
        return FlowStatus(state=FlowState.PENDING)

    def _commit_locked(self, session: _Session, display: str) -> FlowStatus:
        # A RE-AUTH commits into a row that must still be there. Another tab can delete the
        # account while this flow is mid-probe, and both outcomes were wrong: for a harness
        # whose folder goes with it, `commit_account` raised AccountError -- which `poll_flow`
        # does not catch, so a 500 every two seconds for the rest of the deadline, each one
        # re-running the probe under the service lock. For claude the `projects/` husk keeps
        # the folder alive, so the commit SUCCEEDED and silently resurrected the row the user
        # had just deleted, with a fresh seq. Refused as a flow failure: the account is gone
        # because they removed it, and that is an answer, not an error.
        if not session.minted and not accounts.account_exists(session.account_id, self._home):
            self._fail_locked(session, "That account was removed while you were signing in.")
            raise FlowError(session.detail or "account removed")
        if session.lane.harness is HarnessType.CLAUDE and isinstance(session.method, PtyMethod):
            # A key or a subscription token left in the settings env outranks the sign-in that
            # just landed, so a browser sign-in clears them. It is also how an account holding a
            # pasted subscription token stops holding one.
            write_claude_env(accounts.account_dir(session.account_id, self._home), {})
        account = accounts.commit_account(session.account_id, session.lane.id, display, self._home)
        # A re-auth is only worth doing if the chats on that account come back. They do not on
        # their own: claude reads its settings env at process start, and nothing shows codex's
        # daemon re-reading a swapped credential either. One rule for every harness rather than
        # a per-harness table built on untested assumptions -- a restart after a deliberate
        # sign-in is cheap, and being wrong the other way leaves a chat dead with no sign of it.
        if not session.minted:
            # Off-thread, and this is not an optimisation. It runs one `mngr start --restart`
            # per bound agent serially with a 60s timeout each, and this whole method runs
            # under the auth service's single lock -- so an account with eight chats held every
            # poll, submit and abort for eight minutes. The user could not even close the modal,
            # because the abort needs the same lock.
            self._restart_bound_agents(account.id)
        session.state = FlowState.OK
        self._teardown_locked(session, keep_folder=True)
        return FlowStatus(state=FlowState.OK, account_id=account.id)

    def _fail_locked(self, session: _Session, detail: str) -> None:
        session.state = FlowState.FAILED
        session.detail = detail
        self._teardown_locked(session, keep_folder=not session.minted)

    def _teardown_locked(self, session: _Session, keep_folder: bool) -> None:
        if session.timer is not None:
            session.timer.cancel()
            session.timer = None
        if session.login_client is not None:
            session.login_client.close()
            session.login_client = None
        if session.scratch_dir is not None:
            shutil.rmtree(session.scratch_dir, ignore_errors=True)
            session.scratch_dir = None
        if session.process is not None:
            safe_terminate(session.process)
            safe_close(session.process)
            session.process = None
        if not keep_folder:
            accounts.discard_account_dir(session.account_id, self._home)

    def _drop_locked(self) -> None:
        # try/finally, because dropping the session is the part that must not be skipped: this
        # runs as the first statement of `start()`, so a raise in the restore left the old
        # session in place and every later sign-in 500ed until the process restarted.
        try:
            if self._session is not None and self._session.state is FlowState.PENDING:
                # Abandoned rather than failed -- back button, closed modal, a second sign-in
                # displacing this one.
                self._teardown_locked(self._session, keep_folder=not self._session.minted)
        finally:
            self._session = None

    def _expire(self, session: _Session, seconds: float) -> None:
        with self._lock:
            if self._session is session and session.state is FlowState.PENDING:
                logger.info("Sign-in flow {} expired after {}s", session.flow_id, seconds)
                session.state = FlowState.FAILED
                # Which deadline fired changes what the user should do about it.
                session.detail = (
                    "The provider never confirmed that code. Try signing in again."
                    if session.code_submitted
                    else "The sign-in timed out. Start over to get a fresh link."
                )
                # A flow whose last verdict was "the check could not run" is the one case
                # where the folder may hold a completed browser sign-in, and the settle path
                # deliberately keeps it for that reason. Letting the deadline discard it
                # anyway makes the two mechanisms contradict each other.
                keep = not session.minted or session.last_verdict is SignedIn.UNKNOWN
                self._teardown_locked(session, keep_folder=keep)

    def _require_locked(self, flow_id: str, must_be_pending: bool = False) -> _Session:
        if self._session is None or self._session.flow_id != flow_id:
            raise FlowError("that sign-in is no longer active")
        # A settled flow has had its PTY terminated and its process set to None, so anything
        # that would go on to drive it has to be refused here rather than raise on the way.
        if must_be_pending and self._session.state is not FlowState.PENDING:
            raise FlowError(self._session.detail or "that sign-in has already finished")
        return self._session

    def _arm_deadline_locked(self, session: _Session, seconds: float) -> None:
        """Nothing else bounds a flow: the PTY only advances when a client polls.

        Re-arming replaces the running timer, which is how the long browser-round-trip
        budget gets swapped for the short verdict one the moment a code lands.
        """
        if session.timer is not None:
            session.timer.cancel()
        session.timer = threading.Timer(seconds, self._expire, args=(session, seconds))
        session.timer.daemon = True
        session.timer.start()


def _auth_command_signatures() -> set[tuple[str, ...]]:
    """Every (binary, *argv) this service ever spawns, derived from the lane table.

    Derived rather than listed so a new PTY method cannot be forgotten here.
    """
    signatures: set[tuple[str, ...]] = set()
    for lane in LANES:
        for method in lane.methods:
            if isinstance(method, PtyMethod):
                signatures.add((_binary_for(lane), *method.argv))
    return signatures


_APP_SERVER_SIGNATURE: Final = ("codex", "app-server")
_SIGN_IN_FLOW_MARKER: Final = f"{SIGN_IN_FLOW_ENV_VAR}=1".encode()


def reap_orphaned_auth_processes(home: Path | None = None) -> int:
    """Kill sign-in CLIs left running by a previous process. Returns how many.

    A supervisord restart, an OOM kill or a container stop mid-sign-in leaves the pexpect child
    reparented to PID 1 and still running. It is not merely garbage: it still holds the account
    folder open and can still WRITE a credential into it -- into a folder the boot sweep has
    just deleted, or over one whose parked backup was just restored. Folder sweeping does not
    cover it, so this does.

    Matched on the command AND the environment, because either alone is wrong. A chat agent is
    scoped to an account by exactly the same variable, so environment alone would kill the
    user's own agents; and a developer running `codex login` by hand in a terminal has the same
    argv, so command alone would kill that. Only something that is one of OUR spawns, pointed at
    a folder under OUR accounts root, matches both.

    Never raises. This runs at boot and nothing here is worth refusing to start over.
    """
    signatures = _auth_command_signatures()
    # Compared as BYTES. `/proc/<pid>/environ` is whatever the process was given and need not be
    # valid UTF-8, and a lossy decode to go looking for a substring would be inventing
    # characters to answer a question that does not need them.
    scoping_value = f"={accounts.accounts_root(home)}".encode()
    reaped = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = tuple(entry.joinpath("cmdline").read_bytes().decode().split("\0")[:-1])
            if not cmdline:
                continue
            # argv[0] can be an absolute path; compare on the basename.
            signature = (Path(cmdline[0]).name, *cmdline[1:])
            is_app_server = signature[:2] == _APP_SERVER_SIGNATURE
            if signature not in signatures and not is_app_server:
                continue
            environ = entry.joinpath("environ").read_bytes()
            if scoping_value not in environ:
                continue
            # The account's own chats run app-servers on the same codex home; only one this
            # service started for a sign-in carries the marker.
            if is_app_server and _SIGN_IN_FLOW_MARKER not in environ.split(b"\0"):
                continue
            os.kill(int(entry.name), signal.SIGKILL)
            reaped += 1
            logger.warning("Reaped orphaned sign-in process {} ({})", entry.name, " ".join(cmdline))
        except (OSError, UnicodeDecodeError):
            # The process exited under us, or is not ours to read, or its cmdline is not text.
            # None of those is a process we spawned. Deliberately NOT catching ValueError:
            # `int()` cannot fail after the `isdigit` check above, and ValueError is
            # JSONDecodeError's parent, so catching it here would silently swallow a class of
            # corruption this file has no business hiding.
            continue
    return reaped


def _binary_for(lane: Lane) -> str:
    """The CLI a lane drives. The mngr agent type and the binary name differ for two."""
    return {"pi-coding": "pi", "antigravity": "agy"}.get(lane.harness.value, lane.harness.value)


# The prefix of a Claude subscription token, which a paste is refused for: Anthropic's terms
# do not let a third party take in or keep a Claude.ai credential.
_OAUTH_TOKEN_PREFIX: Final = "sk-ant-oat01-"


# What an adopted account is called. Distinct from the lane's own provider name on purpose:
# it is how re-keying finds the row it already owns, and it is the only thing that tells the
# user which of their anthropic accounts came from the keys page rather than a browser sign-in.
ADOPTED_DISPLAY: Final = "Anthropic (Imbue)"


def _adopted_account(lane_id: str, home: Path | None) -> accounts.Account | None:
    """The account a previous adopt created, if its folder is still there.

    Matched on `ADOPTED_DISPLAY` rather than on the lane, because the lane also holds every
    account the user signed into through a browser -- and re-keying must not overwrite one
    of those.
    """
    for account in accounts.read_index(home).accounts:
        if (
            account.lane == lane_id
            and account.display == ADOPTED_DISPLAY
            and accounts.account_dir(account.id, home).is_dir()
        ):
            return account
    return None


def claude_env_from_paste(pasted: str) -> dict[str, str]:
    """The managed settings-env block a claude paste means.

    A bare key is the common case, but the same field takes an env-file paste -- which is
    how a proxied setup arrives, since ANTHROPIC_BASE_URL only means anything alongside its
    key. `parse_credential_lines` is what rejects an unmanaged key or a subscription token,
    so both shapes go through it rather than only the pasted-block one.
    """
    if "=" in pasted:
        return dict(parse_credential_lines(pasted))
    if pasted.startswith(_OAUTH_TOKEN_PREFIX):
        raise CredentialPasteError(SUBSCRIPTION_TOKEN_REFUSAL)
    return {ANTHROPIC_API_KEY_ENV_VAR: pasted}


def write_claude_env(account_path: Path, managed_env: Mapping[str, str]) -> None:
    """Write an account's settings.json env block, replacing every managed key.

    Replaced rather than merged: the block is fully controlled, so a second sign-in that
    dropped a key would otherwise leave the old one behind to outrank the new one.
    """
    settings = account_path / "settings.json"
    existing = json.loads(settings.read_text()) if settings.exists() else {}
    kept = {k: v for k, v in dict(existing.get("env", {})).items() if k not in MANAGED_AUTH_ENV_KEYS}
    existing["env"] = {**kept, **managed_env}
    settings.write_text(json.dumps(existing, indent=2) + "\n")
    # Interactive claude challenges any ANTHROPIC_API_KEY it has not been told about -- a TUI
    # dialog that blocks the agent before it ever signals ready, so `mngr create` destroys it
    # on the readiness timeout. mngr approves keys it can see at creation time; a key that
    # arrives through a sign-in is ours to approve, in this account's own .claude.json.
    record_api_key_approval(managed_env, account_path / ".claude.json")
    settings.chmod(0o600)


def _key_to_check(
    sink: PasteSink, lane: Lane, api_key: str, key_provider: str | None
) -> tuple[CheckedProvider, str, str | None] | None:
    """The provider to ask about a pasted key, the key itself, and the proxy it belongs to, if any.

    None for a key whose provider this build does not check.
    """
    match sink:
        case PasteSink.CLAUDE_ENV:
            managed_env = claude_env_from_paste(api_key)
            key = managed_env.get(ANTHROPIC_API_KEY_ENV_VAR)
            if key is None:
                return None
            return CheckedProvider.ANTHROPIC, key, managed_env.get(ANTHROPIC_BASE_URL_ENV_VAR)
        case PasteSink.CODEX_AUTH_JSON:
            return CheckedProvider.OPENAI, api_key, None
        case PasteSink.PI_AUTH_JSON:
            provider_id = key_provider or (lane.key_providers[0].provider_id if lane.key_providers else lane.id)
            if provider_id not in {provider.value for provider in CheckedProvider}:
                return None
            return CheckedProvider(provider_id), api_key, None
        case _ as unreachable:
            assert_never(unreachable)


def _credential_paths(sink: PasteSink, account_path: Path) -> tuple[Path, ...]:
    """The files a sink writes, so a rejected credential can be rolled back."""
    match sink:
        # pi and codex both name their credential auth.json; what differs is the shape inside it.
        case PasteSink.PI_AUTH_JSON | PasteSink.CODEX_AUTH_JSON:
            return (account_path / "auth.json",)
        case PasteSink.CLAUDE_ENV:
            return (account_path / "settings.json",)
        case _ as unreachable:
            assert_never(unreachable)


def _read_credentials(paths: Sequence[Path]) -> dict[Path, bytes | None]:
    """The bytes of an account's credential files, or None where the file is absent."""
    return {path: (path.read_bytes() if path.exists() else None) for path in paths}


# How much PTY transcript a session keeps. Every poll appends a second of output and every
# failure pattern is re-scanned over the whole string, so an unbounded buffer is both memory and
# CPU that grows with how long the user takes. agy is the case that showed it: 1000 columns of
# animating TUI, captured for as long as the sign-in page is open. The patterns and the value
# scrapes all read recent output, so keeping the tail loses nothing.
_MAX_SESSION_OUTPUT_CHARS: Final = 64_000


def _bounded(output: str) -> str:
    """The tail of `output`, capped. Whole-string ops downstream stay O(cap)."""
    return output if len(output) <= _MAX_SESSION_OUTPUT_CHARS else output[-_MAX_SESSION_OUTPUT_CHARS:]


def _restore_credentials(before: Mapping[Path, bytes | None]) -> None:
    """Put back what `_read_credentials` saw.

    Snapshot-and-restore rather than write-to-temp-then-move: a sink may merge with what is
    already there (claude's settings.json keeps every unmanaged key), so the new content is
    not derivable without writing it, and only the previous bytes are worth keeping.
    """
    for path, content in before.items():
        # The folder can be gone: another client may have deleted the account while this flow
        # held its credential. There is nothing to restore into, and raising here would happen
        # BEFORE `self._session = None`, wedging every later sign-in to any provider.
        if not path.parent.is_dir():
            continue
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)
            path.chmod(0o600)


@contextlib.contextmanager
def _credentials_restored_on_error(paths: Sequence[Path]) -> Iterator[Mapping[Path, bytes | None]]:
    """Write inside this, and an exception leaves the previous credential in place.

    The probe cannot judge a file that is not there, so the write has to land first -- and on
    a re-auth that folder is a LIVE account. A half-written credential left behind breaks
    every agent bound to it, silently, at its next turn.
    """
    before = _read_credentials(paths)
    try:
        yield before
    except Exception:
        _restore_credentials(before)
        raise


def _write_paste(sink: PasteSink, account_path: Path, api_key: str, key_provider: str | None, lane: Lane) -> str:
    """Write a pasted credential and return the provider noun the account is named after."""
    match sink:
        case PasteSink.PI_AUTH_JSON:
            provider_id = key_provider or (lane.key_providers[0].provider_id if lane.key_providers else lane.id)
            display = next((k.display for k in lane.key_providers if k.provider_id == provider_id), lane.provider_name)
            path = account_path / "auth.json"
            # One provider per folder is our rule, not pi's -- pi's auth.json is a map and would
            # happily hold several. Writing exactly one is what keeps an account's model list
            # scoped to the provider its row claims.
            path.write_text(json.dumps({provider_id: {"type": "api_key", "key": api_key}}, indent=2) + "\n")
            path.chmod(0o600)
            return display
        case PasteSink.CODEX_AUTH_JSON:
            path = account_path / "auth.json"
            # The same file the device flow ends up writing, in the shape codex reads as
            # API-key mode: `auth_mode` spelled the way codex serialises it, and no `tokens`.
            # The key alone would resolve the same way; naming the mode leaves nothing inferred.
            path.write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": api_key}, indent=2) + "\n")
            path.chmod(0o600)
            return lane.provider_name
        case PasteSink.CLAUDE_ENV:
            write_claude_env(account_path, claude_env_from_paste(api_key))
            return lane.provider_name
        case _ as unreachable:
            assert_never(unreachable)
