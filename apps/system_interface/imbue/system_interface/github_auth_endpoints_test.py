"""Integration tests for the /api/github-auth/* endpoints.

Each test builds a `GitHubAuthService` with deterministic fakes and passes
it to `create_application`, which stores it on the app's
`SystemInterfaceState` for the handlers to read. This exercises the
GitHub-auth recovery paths end-to-end through the Flask test client without
touching a real `gh` binary -- and without `unittest.mock` or runtime
attribute patching.

The behavioral contract these tests pin (from the known-correct build
methods):

- The PAT path feeds the token over a REAL stdin pipe
  (`command_runner(..., input=token + "\\n")`), never via a PTY, and treats
  `returncode == 0` as success. A nonzero login exit raises rather than
  reporting a bogus success.
- After ANY successful login (PAT or web/device), the service ALWAYS runs
  `gh auth setup-git --hostname github.com` so the git credential helper is
  wired -- this is what makes `git push` work with no agent restart.
- No agent restart ever happens: no `mngr stop` / `mngr start` is invoked.
- The web/device flow sends a leading newline (to answer gh's possible
  "Press Enter to open in browser" prompt) before parsing the user code and
  verification URL.
- `POST /api/github-auth/require` broadcasts `{"type": "github_auth_required"}`.
- Every route is loopback-guarded: a non-loopback `remote_addr` gets 403.
"""

from __future__ import annotations

import json
import queue
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager

from flask.testing import FlaskClient

from imbue.system_interface.app_context import state_of
from imbue.system_interface.github_auth import GitHubAuthService
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import FakeFinishedProcess

# A token the PAT path pipes over stdin; the test asserts it arrives verbatim
# (with the trailing newline `gh auth login --with-token` reads to EOF).
_FAKE_PAT = "ghp_faketoken000000000000000000000000"

# What `gh auth status` prints for a logged-in account: the "Logged in to
# github.com account <name>" line the service's status parser keys on. The
# assertions key on the resulting `logged_in` signal, not a specific parsed
# username, so they stay robust to the exact username-extraction regex.
_GH_STATUS_LOGGED_IN = (
    "github.com\n"
    "  ✓ Logged in to github.com account octocat (keyring)\n"
    "  - Active account: true\n"
    "  - Git operations protocol: https\n"
)

# The device/web flow surfaces a `XXXX-XXXX` user code and a device URL. The
# fake pexpect process leaves these in its consumed buffer for the service's
# lenient regexes to pull out.
_FAKE_USER_CODE = "ABCD-1234"
_FAKE_VERIFICATION_URL = "https://github.com/login/device"


class _RecordingCommandRunner:
    """A `command_runner` fake that records every invocation, including stdin.

    The github command-runner seam accepts an optional `input=` kwarg (the
    PAT path pipes the token over a real stdin pipe). This fake records the
    argv and the piped stdin for each call so tests can assert (a) the token
    was fed over stdin, (b) `gh auth setup-git` ran, and (c) no `mngr
    stop`/`mngr start` (agent restart) was ever invoked.

    `login_returncode` controls the `gh auth login --with-token` exit so a
    test can drive the success (0) and rejection (nonzero) branches.
    """

    def __init__(self, login_returncode: int = 0) -> None:
        self._login_returncode = login_returncode
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(
        self,
        command: list[str],
        timeout: float,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
    ) -> FakeFinishedProcess:
        self.calls.append((tuple(command), input))
        if command[:3] == ["gh", "auth", "login"]:
            return FakeFinishedProcess(returncode=self._login_returncode)
        if command[:3] == ["gh", "auth", "setup-git"]:
            return FakeFinishedProcess(returncode=0)
        if command[:3] == ["gh", "auth", "status"]:
            return FakeFinishedProcess(stdout=_GH_STATUS_LOGGED_IN, stderr=_GH_STATUS_LOGGED_IN, returncode=0)
        return FakeFinishedProcess(returncode=0)

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        return [argv for argv, _ in self.calls]

    def stdin_for(self, prefix: tuple[str, ...]) -> str | None:
        """Return the stdin piped to the first call whose argv starts with `prefix`."""
        for argv, stdin in self.calls:
            if argv[: len(prefix)] == prefix:
                return stdin
        return None

    def was_called(self, prefix: tuple[str, ...]) -> bool:
        return any(argv[: len(prefix)] == prefix for argv in self.argvs)


class _FakeGitHubWebProcess:
    """A pexpect-spawn stand-in for the `gh auth login --web` device flow.

    Records the leading `sendline("")` the service sends to answer gh's
    possible "Press Enter to open in browser" prompt, and presets the
    consumed buffer (`before` + `after`) with a `XXXX-XXXX` user code and a
    device verification URL so the service's lenient regexes extract them.
    Every `expect()` returns index 0 (the user-code / EOF branch), matching
    how the URL-found and login-complete paths land.
    """

    def __init__(self) -> None:
        self.sendline_calls: list[str] = []
        self.terminate_calls = 0
        self.close_calls = 0
        self.timeout: float | None = None
        self.before = f"! First copy your one-time code: {_FAKE_USER_CODE}\n"
        self.after = f"Open this URL to continue: {_FAKE_VERIFICATION_URL}\n"

    def expect(self, _patterns: object) -> int:
        return 0

    def sendline(self, s: str) -> None:
        self.sendline_calls.append(s)

    def isalive(self) -> bool:
        return True

    def terminate(self, force: bool = False) -> None:
        self.terminate_calls += 1

    def close(self) -> None:
        self.close_calls += 1


@contextmanager
def _client(github_auth_service: GitHubAuthService) -> Iterator[FlaskClient]:
    """Build a Flask test client over an app wired with the given service."""
    app = create_application(github_auth_service=github_auth_service)
    yield app.test_client()


def _drain(client_queue: "queue.Queue[str | None]") -> list[dict[str, object]]:
    """Decode every JSON message currently enqueued on a broadcaster client queue.

    The broadcaster serializes each message to JSON and pushes it onto every
    registered client queue; the caller registers the queue before the
    endpoint runs so the broadcast lands here to be read back. The endpoint's
    synchronous POST has already returned by the time this runs, so there is
    no concurrent producer and ``empty()`` is an exact drain condition.
    """
    messages: list[dict[str, object]] = []
    while not client_queue.empty():
        text = client_queue.get_nowait()
        if text is not None:
            messages.append(json.loads(text))
    return messages


def test_submit_raw_token_pipes_token_over_stdin_and_wires_git() -> None:
    """The PAT path feeds the token over stdin, then wires the git credential helper.

    Asserts the three load-bearing invariants of the PAT path:
      1. `gh auth login --with-token` receives the token over a REAL stdin
         pipe (`input=`), with the trailing newline it reads to EOF.
      2. `gh auth setup-git --hostname github.com` runs after the login so
         `git push` works with no agent restart.
      3. returncode==0 → the endpoint reports `logged_in: true`.
    """
    runner = _RecordingCommandRunner(login_returncode=0)
    service = GitHubAuthService(command_runner=runner)
    with _client(service) as client:
        response = client.post("/api/github-auth/submit-raw-token", json={"token": _FAKE_PAT})
    assert response.status_code == 200
    assert response.get_json()["logged_in"] is True

    assert runner.stdin_for(("gh", "auth", "login")) == _FAKE_PAT + "\n"
    assert runner.was_called(("gh", "auth", "setup-git", "--hostname", "github.com"))
    # The token must never be passed as an argv element (that would leak it
    # into the process table); it rides stdin only.
    for argv, _stdin in runner.calls:
        assert _FAKE_PAT not in argv


def test_submit_raw_token_performs_no_agent_restart() -> None:
    """No `mngr stop` / `mngr start` is ever invoked on the PAT path.

    Unlike the Claude API-key path, a gh login needs no agent restart: the
    git credential helper is wired live via `gh auth setup-git`, so a running
    agent's `git push` picks up the new credential without being respawned.
    """
    runner = _RecordingCommandRunner(login_returncode=0)
    service = GitHubAuthService(command_runner=runner)
    with _client(service) as client:
        response = client.post("/api/github-auth/submit-raw-token", json={"token": _FAKE_PAT})
    assert response.status_code == 200
    assert all(argv[:2] != ("mngr", "stop") for argv in runner.argvs)
    assert all(argv[:2] != ("mngr", "start") for argv in runner.argvs)


def test_submit_raw_token_nonzero_login_does_not_report_success() -> None:
    """A nonzero `gh auth login` exit must not masquerade as a successful login.

    Success is defined by `returncode == 0`, not by parsing output. When the
    login exits nonzero the service raises, the endpoint surfaces a non-200,
    and no success (`logged_in: true`) is reported. `gh auth setup-git` must
    NOT run for a failed login (there is no credential to wire).
    """
    runner = _RecordingCommandRunner(login_returncode=1)
    service = GitHubAuthService(command_runner=runner)
    with _client(service) as client:
        response = client.post("/api/github-auth/submit-raw-token", json={"token": _FAKE_PAT})
    assert response.status_code >= 400
    body = response.get_json()
    assert body.get("logged_in") is not True
    assert not runner.was_called(("gh", "auth", "setup-git"))


def test_submit_raw_token_rejects_empty_token() -> None:
    """An empty / whitespace-only token is a 400 before any subprocess runs."""
    runner = _RecordingCommandRunner()
    service = GitHubAuthService(command_runner=runner)
    with _client(service) as client:
        response = client.post("/api/github-auth/submit-raw-token", json={"token": "   "})
    assert response.status_code == 400
    assert runner.calls == []


def test_start_web_sends_leading_newline_and_returns_parsed_code_and_url() -> None:
    """The device/web flow answers the "Press Enter" prompt and parses code + URL.

    gh may print "Press Enter to open in browser" before the code/URL, so the
    service sends a leading newline first. The parsed `user_code` and
    `verification_url` come back to the caller for the user to complete the
    device login.
    """
    fake_process = _FakeGitHubWebProcess()
    runner = _RecordingCommandRunner(login_returncode=0)
    service = GitHubAuthService(command_runner=runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    with _client(service) as client:
        response = client.post("/api/github-auth/start", json={})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["user_code"] == _FAKE_USER_CODE
    assert payload["verification_url"] == _FAKE_VERIFICATION_URL
    assert payload["session_id"]
    # The leading newline that answers gh's possible "Press Enter" prompt.
    assert fake_process.sendline_calls[:1] == [""]


def test_submit_code_wires_git_and_reports_status() -> None:
    """Completing the device flow runs `gh auth setup-git` and reports auth status.

    `submit-code` waits for the login subprocess to finish, then ALWAYS wires
    the git credential helper (same invariant as the PAT path) before
    returning the parsed auth status.
    """
    fake_process = _FakeGitHubWebProcess()
    runner = _RecordingCommandRunner(login_returncode=0)
    service = GitHubAuthService(command_runner=runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    with _client(service) as client:
        start = client.post("/api/github-auth/start", json={})
        assert start.status_code == 200
        session_id = start.get_json()["session_id"]
        submit = client.post("/api/github-auth/submit-code", json={"session_id": session_id})
    assert submit.status_code == 200
    assert submit.get_json()["logged_in"] is True
    assert runner.was_called(("gh", "auth", "setup-git", "--hostname", "github.com"))
    assert all(argv[:2] != ("mngr", "stop") for argv in runner.argvs)
    assert all(argv[:2] != ("mngr", "start") for argv in runner.argvs)


def test_require_broadcasts_github_auth_required() -> None:
    """POST /api/github-auth/require broadcasts the modal-open event.

    The `/publish-inspiration` skill hits this endpoint when its own `gh auth
    status` check fails, so the frontend opens the GitHub-login modal. The
    event must reach the WebSocket broadcaster verbatim.
    """
    runner = _RecordingCommandRunner()
    service = GitHubAuthService(command_runner=runner)
    app = create_application(github_auth_service=service)
    broadcaster = state_of(app).broadcaster
    # Register a client before the POST so the broadcast lands on its queue.
    client_queue = broadcaster.register()
    response = app.test_client().post("/api/github-auth/require", json={})
    assert response.status_code == 200
    messages = _drain(client_queue)
    broadcaster.unregister(client_queue)
    assert {"type": "github_auth_required"} in messages


def test_status_endpoint_returns_parsed_status() -> None:
    """GET /api/github-auth/status parses `gh auth status` into the wire model."""
    runner = _RecordingCommandRunner()
    service = GitHubAuthService(command_runner=runner)
    with _client(service) as client:
        response = client.get("/api/github-auth/status")
    assert response.status_code == 200
    assert response.get_json()["logged_in"] is True
    assert runner.was_called(("gh", "auth", "status"))


def test_every_route_rejects_non_loopback_caller() -> None:
    """Every /api/github-auth/* route is loopback-guarded (handles credentials).

    A caller whose `remote_addr` is not loopback gets 403 on every route --
    these handle a pasted PAT, trigger the device flow, and open the login
    modal, so none may be reachable off-box.
    """
    runner = _RecordingCommandRunner()
    service = GitHubAuthService(command_runner=runner)
    non_loopback = {"REMOTE_ADDR": "10.0.0.5"}
    with _client(service) as client:
        results = [
            client.get("/api/github-auth/status", environ_base=non_loopback),
            client.post("/api/github-auth/start", json={}, environ_base=non_loopback),
            client.post("/api/github-auth/submit-code", json={"session_id": "x"}, environ_base=non_loopback),
            client.post("/api/github-auth/submit-raw-token", json={"token": _FAKE_PAT}, environ_base=non_loopback),
            client.post("/api/github-auth/require", json={}, environ_base=non_loopback),
            client.post("/api/github-auth/abort", json={}, environ_base=non_loopback),
        ]
    assert [r.status_code for r in results] == [403, 403, 403, 403, 403, 403]
    # A rejected caller must never reach a subprocess.
    assert runner.calls == []
