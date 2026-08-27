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

import json
import os
import re
import threading
import uuid
from collections.abc import Callable
from enum import StrEnum
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

import pexpect
from loguru import logger as _loguru_logger

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface import accounts
from imbue.system_interface.harnesses.binding import account_env
from imbue.system_interface.harnesses.binding import adopt_default_claude_home
from imbue.system_interface.harnesses.binding import seed_account
from imbue.system_interface.harnesses.claude.auth import ANTHROPIC_API_KEY_ENV_VAR
from imbue.system_interface.harnesses.claude.auth import MANAGED_AUTH_ENV_KEYS
from imbue.system_interface.harnesses.claude.auth import parse_credential_lines
from imbue.system_interface.harnesses.lanes import DrainUntil
from imbue.system_interface.harnesses.lanes import EofPolicy
from imbue.system_interface.harnesses.lanes import Lane
from imbue.system_interface.harnesses.lanes import PasteMethod
from imbue.system_interface.harnesses.lanes import PasteSink
from imbue.system_interface.harnesses.lanes import PtyMethod
from imbue.system_interface.harnesses.lanes import Scrape
from imbue.system_interface.harnesses.lanes import Submit
from imbue.system_interface.harnesses.lanes import LANE_ANTHROPIC
from imbue.system_interface.harnesses.lanes import get_lane
from imbue.system_interface.harnesses.lanes import get_method
from imbue.system_interface.harnesses.pty_auth import PtyAuthError
from imbue.system_interface.harnesses.pty_auth import drain_pty_stream
from imbue.system_interface.harnesses.pty_auth import drain_pty_stream_until_quiet
from imbue.system_interface.harnesses.pty_auth import extract_hyperlink_value
from imbue.system_interface.harnesses.pty_auth import extract_wrapped_value
from imbue.system_interface.harnesses.pty_auth import safe_close
from imbue.system_interface.harnesses.pty_auth import safe_terminate
from imbue.system_interface.harnesses.pty_auth import spawn_pty
from imbue.system_interface.harnesses.signed_in import SignedIn
from imbue.system_interface.harnesses.signed_in import is_signed_in

logger = _loguru_logger

# The CLI's input treats a rapid burst as a paste, so Enter must arrive as its own later
# keystroke or it lands in the field as content.
_CODE_ECHO_QUIET_SECONDS: Final = 0.3
_CODE_ECHO_DEADLINE_SECONDS: Final = 3.0
_READY_WAIT_SECONDS: Final = 20.0
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


class FlowStart(FrozenModel):
    flow_id: str
    shape: FlowShape
    url: str | None = None
    code: str | None = None


class FlowStatus(FrozenModel):
    state: FlowState
    detail: str | None = None
    account_id: str | None = None


def flow_shape(method: PtyMethod | PasteMethod) -> FlowShape:
    if isinstance(method, PasteMethod):
        return FlowShape.PASTE
    return FlowShape.CODE_THEN_WAIT if method.submit is Submit.NONE else FlowShape.URL_THEN_CODE


def _never_done(_buffer: str) -> bool:
    """Drain to EOF: the value only appears as the CLI exits."""
    return False



def _extract(raw: str, scrape: Scrape, frame_marker: str | None) -> str | None:
    """Recover a scraped value, preferring an OSC 8 hyperlink target when there is one."""
    strict = re.compile(scrape.strict)
    from_link = extract_hyperlink_value(raw, strict)
    if from_link is not None:
        return from_link
    value = extract_wrapped_value(
        raw, strict, re.compile(scrape.continuation), frame_marker=frame_marker
    )
    if value is not None and scrape.min_length is not None and len(value) < scrape.min_length:
        # A short extraction is a wrapped fragment, not the value -- keep draining.
        return None
    return value


class _Session:
    """One live flow. Mutable by design; guarded by the service's lock."""

    flow_id: str
    lane: Lane
    method: PtyMethod | PasteMethod
    account_id: str
    process: Any
    output: str
    state: FlowState
    detail: str | None
    timer: threading.Timer | None

    def is_value_ready(self, buffer: str) -> bool:
        """Whether the scraped value can be read yet -- the drain loop's stop condition.

        A bound method rather than a closure over the method: the drain loop needs a
        predicate, and this is the one piece of per-flow state it has to see.
        """
        method = self.method
        if isinstance(method, PasteMethod):
            return True
        return _extract(buffer, method.scrape, method.frame_marker) is not None


def _new_session(lane: Lane, method: PtyMethod | PasteMethod, account_id: str) -> _Session:
    session = _Session()
    session.flow_id = uuid.uuid4().hex
    session.lane = lane
    session.method = method
    session.account_id = account_id
    session.process = None
    session.output = ""
    session.state = FlowState.PENDING
    session.detail = None
    session.timer = None
    return session


class AuthFlowService:
    """Starts, advances, and tears down sign-in flows."""

    _home: Path | None
    _work_dir: Path
    _lock: threading.Lock
    _session: _Session | None
    _spawner: Callable[..., Any]

    @classmethod
    def create(
        cls,
        home: Path | None = None,
        work_dir: Path | None = None,
        spawner: Callable[..., Any] | None = None,
    ) -> "AuthFlowService":
        """`spawner` stands in for `spawn_pty`, so a test can drive a scripted terminal.

        Injected rather than patched, matching ClaudeAuthService's `pexpect_spawner`.
        """
        service = cls()
        service._home = home
        service._work_dir = work_dir or Path("/home/user/workspace")
        service._lock = threading.Lock()
        service._session = None
        service._spawner = spawner or spawn_pty
        return service

    # -- lifecycle ------------------------------------------------------------------------

    def start(self, lane_id: str, method_id: str, account_id: str | None = None) -> FlowStart:
        """Begin a sign-in. Any flow already running is abandoned.

        `account_id` re-authenticates into an EXISTING folder, so every agent already bound
        to it recovers. Without it a fresh folder is minted.
        """
        lane = get_lane(lane_id)
        method = get_method(lane_id, method_id)

        with self._lock:
            self._drop_locked()
            if account_id is None:
                account_id, account_path = accounts.mint_account_dir(self._home)
            else:
                account_path = accounts.account_dir(account_id, self._home)
                if not account_path.is_dir():
                    raise FlowError(f"no such account: {account_id}")
            seed_account(lane.harness, account_path, self._work_dir)

            if isinstance(method, PasteMethod):
                # Nothing to drive; the caller supplies the credential on submit.
                self._session = _new_session(lane, method, account_id)
                return FlowStart(flow_id=self._session.flow_id, shape=FlowShape.PASTE)

            session = _new_session(lane, method, account_id)
            self._session = session
            url, code = self._drive_locked(session, method, account_path)
            self._arm_deadline_locked(session, method.flow_deadline_s)
            return FlowStart(
                flow_id=session.flow_id,
                shape=flow_shape(method),
                url=method.static_url or url,
                code=code,
            )

    def _drive_locked(
        self, session: _Session, method: PtyMethod, account_path: Path
    ) -> tuple[str | None, str | None]:
        """Spawn the CLI, get it to the point of showing something, and scrape it."""
        env = {**os.environ, **account_env(session.lane.harness, account_path)}
        binary = _binary_for(session.lane)
        session.process = self._spawner(binary, list(method.argv), method.scrape_timeout_s, env=env)

        # A keystroke script is blind without this: a reordered menu would make the same keys
        # choose a different login method, and nothing downstream would notice.
        if method.expect_before_keys is not None:
            if session.process.expect(
                [re.compile(method.expect_before_keys), pexpect.EOF, pexpect.TIMEOUT],
                timeout=_READY_WAIT_SECONDS,
            ) != 0:
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
            index = session.process.expect(
                [re.compile(method.scrape.trigger), pexpect.EOF, pexpect.TIMEOUT],
                timeout=method.scrape_timeout_s,
            )
            if index != 0:
                self._fail_locked(session, "Timed out waiting for the sign-in details.")
                raise FlowError(session.detail or "no value")
            session.output += (session.process.before or "") + (session.process.after or "")
        # A URL is drained until it can be extracted -- the CLI animates forever afterwards,
        # so there is no quiet gap to wait for. A minted token is drained to process exit,
        # because the CLI prints it and leaves.
        is_done: Callable[[str], bool] = (
            _never_done if method.scrape.drain_until is DrainUntil.EOF else session.is_value_ready
        )
        session.output = drain_pty_stream(session.process, session.output, is_done)
        value = _extract(session.output, method.scrape, method.frame_marker)
        if value is None:
            self._fail_locked(session, "Could not read the sign-in details from the terminal.")
            raise FlowError(session.detail or "extraction failed")
        return (None, value) if method.static_url else (value, None)

    # -- advancing ------------------------------------------------------------------------

    def submit_code(self, flow_id: str, code: str) -> FlowStatus:
        with self._lock:
            session = self._require_locked(flow_id)
            method = session.method
            if isinstance(method, PasteMethod):
                raise FlowError("this sign-in does not take a code")
            if method.submit is Submit.NONE:
                raise FlowError("this sign-in does not take a code")
            # Two writes: the code, then Enter separately, or the paste heuristic swallows it.
            session.process.send(code)
            session.output = drain_pty_stream_until_quiet(
                session.process, session.output, _CODE_ECHO_QUIET_SECONDS, _CODE_ECHO_DEADLINE_SECONDS
            )
            session.process.send("\r")
            return self._settle_locked(session, method)

    def submit_key(self, flow_id: str, api_key: str, key_provider: str | None = None) -> FlowStatus:
        with self._lock:
            session = self._require_locked(flow_id)
            method = session.method
            if not isinstance(method, PasteMethod):
                raise FlowError("this sign-in does not take a key")
            path = accounts.account_dir(session.account_id, self._home)
            display = _write_paste(method.sink, path, api_key, key_provider, session.lane)
            return self._commit_locked(session, display)

    def adopt_claude_credentials(self, pasted: str) -> accounts.Account:
        """Mint an account from a credential someone else obtained, with no flow involved.

        The Imbue path: the Electron chrome sends what the keys page handed the user. There
        is no terminal to drive and nothing to poll, so this skips the flow machinery and
        goes straight to seed, write, commit -- the account existing IS the signed-in flag.
        """
        managed_env = claude_env_from_paste(pasted)
        lane = get_lane("anthropic")
        with self._lock:
            account_id, path = accounts.mint_account_dir(self._home)
            seed_account(lane.harness, path, self._work_dir)
            write_claude_env(path, managed_env)
            account = accounts.commit_account(account_id, lane.id, lane.provider_name, self._home)
            self._adopt_if_first_claude_locked(account)
            return account

    def poll(self, flow_id: str) -> FlowStatus:
        with self._lock:
            session = self._require_locked(flow_id)
            if session.state is not FlowState.PENDING:
                return FlowStatus(state=session.state, detail=session.detail, account_id=session.account_id)
            method = session.method
            if isinstance(method, PasteMethod):
                return FlowStatus(state=FlowState.PENDING)
            return self._settle_locked(session, method)

    def abort(self, flow_id: str) -> None:
        with self._lock:
            if self._session is not None and self._session.flow_id == flow_id:
                self._drop_locked()

    # -- internals ------------------------------------------------------------------------

    def _settle_locked(self, session: _Session, method: PtyMethod) -> FlowStatus:
        """Read what the CLI has said so far and decide, without blocking on it."""
        session.output = drain_pty_stream(session.process, session.output, lambda _: False, deadline_seconds=1.0)

        for pattern, copy in method.failures:
            match = re.search(pattern, session.output)
            if match is not None:
                detail = copy.replace("{1}", match.group(1)) if match.groups() else copy
                self._fail_locked(session, detail)
                return FlowStatus(state=FlowState.FAILED, detail=detail)

        alive = bool(session.process is not None and session.process.isalive())
        said_success = method.success is not None and re.search(method.success, session.output) is not None
        exited_meaning_success = not alive and method.eof_policy is EofPolicy.SUCCESS
        if not (said_success or exited_meaning_success or not alive):
            return FlowStatus(state=FlowState.PENDING)

        # The CLI is done talking. Its own probe, not the screen, decides.
        path = accounts.account_dir(session.account_id, self._home)
        verdict = is_signed_in(session.lane.harness, path)
        if verdict is SignedIn.YES:
            return self._commit_locked(session, session.lane.provider_name)
        if verdict is SignedIn.UNKNOWN:
            # Keep the folder: a network blink is not evidence the sign-in failed, and the
            # user may have just finished a browser round trip we would be throwing away.
            return FlowStatus(state=FlowState.PENDING)
        if method.eof_policy is EofPolicy.FAILURE and not alive:
            self._fail_locked(session, "The sign-in did not complete.")
            return FlowStatus(state=FlowState.FAILED, detail=session.detail)
        return FlowStatus(state=FlowState.PENDING)

    def _commit_locked(self, session: _Session, display: str) -> FlowStatus:
        account = accounts.commit_account(session.account_id, session.lane.id, display, self._home)
        self._adopt_if_first_claude_locked(account)
        session.state = FlowState.OK
        self._teardown_locked(session, keep_folder=True)
        return FlowStatus(state=FlowState.OK, account_id=account.id)

    def _adopt_if_first_claude_locked(self, account: accounts.Account) -> None:
        """Give the agentless claude callers a real credential to resolve.

        Workspace terminals, supervisord services and `claude_p.py` all read `~/.claude`
        with no agent to bind, so the FIRST claude account becomes theirs. Only the first:
        later ones are additional accounts, and silently repointing the default under them
        would move what every skill runs as.
        """
        if account.lane != LANE_ANTHROPIC.id or account.seq != 1:
            return
        adopt_default_claude_home(accounts.account_dir(account.id, self._home), self._home)

    def _fail_locked(self, session: _Session, detail: str) -> None:
        session.state = FlowState.FAILED
        session.detail = detail
        self._teardown_locked(session, keep_folder=False)

    def _teardown_locked(self, session: _Session, keep_folder: bool) -> None:
        if session.timer is not None:
            session.timer.cancel()
            session.timer = None
        if session.process is not None:
            safe_terminate(session.process)
            safe_close(session.process)
            session.process = None
        if not keep_folder:
            accounts.discard_account_dir(session.account_id, self._home)

    def _drop_locked(self) -> None:
        if self._session is not None and self._session.state is FlowState.PENDING:
            self._teardown_locked(self._session, keep_folder=False)
        self._session = None

    def _expire(self, session: _Session, seconds: float) -> None:
        with self._lock:
            if self._session is session and session.state is FlowState.PENDING:
                logger.info("Sign-in flow {} expired after {}s", session.flow_id, seconds)
                self._fail_locked(session, "The sign-in timed out. Start over to get a fresh link.")

    def _require_locked(self, flow_id: str) -> _Session:
        if self._session is None or self._session.flow_id != flow_id:
            raise FlowError("that sign-in is no longer active")
        return self._session

    def _arm_deadline_locked(self, session: _Session, seconds: float) -> None:
        """Nothing else bounds a flow: the PTY only advances when a client polls."""
        session.timer = threading.Timer(seconds, self._expire, args=(session, seconds))
        session.timer.daemon = True
        session.timer.start()


def _binary_for(lane: Lane) -> str:
    """The CLI a lane drives. The mngr agent type and the binary name differ for two."""
    return {"pi-coding": "pi", "antigravity": "agy"}.get(lane.harness.value, lane.harness.value)


def claude_env_from_paste(pasted: str) -> dict[str, str]:
    """The managed settings-env block a claude paste means.

    A bare key is the common case, but the same field takes an env-file paste -- which is
    how a proxied setup arrives, since ANTHROPIC_BASE_URL only means anything alongside its
    key. `parse_credential_lines` is what rejects an unmanaged key or a token mixed with a
    key, so both shapes go through it rather than only the pasted-block one.
    """
    if "=" in pasted:
        return dict(parse_credential_lines(pasted))
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
    settings.chmod(0o600)


def _write_paste(
    sink: PasteSink, account_path: Path, api_key: str, key_provider: str | None, lane: Lane
) -> str:
    """Write a pasted credential and return the provider noun the account is named after."""
    if sink is PasteSink.PI_AUTH_JSON:
        provider_id = key_provider or (lane.key_providers[0].provider_id if lane.key_providers else lane.id)
        display = next(
            (k.display for k in lane.key_providers if k.provider_id == provider_id), lane.provider_name
        )
        path = account_path / "auth.json"
        # One provider per folder is our rule, not pi's -- pi's auth.json is a map and would
        # happily hold several. Writing exactly one is what keeps an account's model list
        # scoped to the provider its row claims.
        path.write_text(json.dumps({provider_id: {"type": "api_key", "key": api_key}}, indent=2) + "\n")
        path.chmod(0o600)
        return display
    if sink is PasteSink.CLAUDE_ENV:
        write_claude_env(account_path, claude_env_from_paste(api_key))
        return lane.provider_name
    raise FlowError(f"{sink} has no writer yet")
