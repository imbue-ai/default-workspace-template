"""Unit tests for the in-mind GitHub auth flows.

Two areas are pinned here:

1. `_gh_child_env`: `gh` prioritizes `GH_TOKEN` / `GITHUB_TOKEN` (and
   enterprise variants) over its credential store, and the system_interface
   process inherits `GH_TOKEN` from the agent environment. If those are left
   in the child environment, `gh auth login` refuses to persist a new
   credential and `gh auth status` reports the env token instead of the
   store, so the login modal can never write a durable credential. These
   tests pin that every such variable is stripped from the child environment
   while the parent process environment is left untouched.

2. The web/device login flow against the REAL gh 2.95 PTY transcript
   (captured inside a minds container). The key regression pinned here: gh
   opens with a terminal-query handshake that swallows any input sent before
   the one-time code appears, so the service must not write to gh's stdin
   until the code and verification URL have been captured -- an early
   newline never reaches the "Press Enter to open ... in your browser"
   prompt and gh then never polls for the authorization.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

import pexpect
import pytest

from imbue.system_interface.github_auth import _GH_TOKEN_ENV_VARS
from imbue.system_interface.github_auth import GitHubAuthError
from imbue.system_interface.github_auth import GitHubAuthService
from imbue.system_interface.github_auth import _gh_child_env
from imbue.system_interface.testing import FakeFinishedProcess

# The verbatim PTY output of `gh auth login --hostname github.com --web
# --git-protocol https` from gh 2.95.0, captured inside a minds container.
# It opens with a terminal-query handshake (OSC 11 background-color query +
# DSR cursor-position query), then prints the one-time code and the "Press
# Enter" prompt containing the verification URL -- the code and URL arrive
# BEFORE any prompt.
_REAL_GH_WEB_TTY_TRANSCRIPT = (
    "\x1b]11;?\x1b\\\x1b[6n\r\n"
    "\x1b[0;33m!\x1b[0m First copy your one-time code: \x1b[0;1;39m9AF8-2E96\x1b[0m\r\n"
    "\x1b[0;1;39mPress Enter\x1b[0m to open https://github.com/login/device in your browser... "
)

# What the same gh printed right after Enter was pressed in the headless
# container: the browser-open attempt fails harmlessly and gh moves on to
# polling for the authorization.
_REAL_GH_WEB_POST_ENTER_OUTPUT = (
    "\r\n\x1b[0;31m!\x1b[0m Failed opening a web browser at https://github.com/login/device\r\n"
    '  exec: "xdg-open,x-www-browser,www-browser,wslview": executable file not found in $PATH\r\n'
    "  Please try entering the URL in your browser manually\r\n"
)

_REAL_USER_CODE = "9AF8-2E96"
_REAL_VERIFICATION_URL = "https://github.com/login/device"

# `gh auth status` output modeled on the gh 2.95 format strings embedded in
# the binary ("  %s Logged in to %s account %s (%s)" and
# "  - Token scopes: %s", scopes single-quoted and comma-separated).
_GH_STATUS_WITH_WORKFLOW_SCOPE = (
    "github.com\n"
    "  Logged in to github.com account octocat (keyring)\n"
    "  - Active account: true\n"
    "  - Git operations protocol: https\n"
    "  - Token: gho_************************************\n"
    "  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'\n"
)

_GH_STATUS_WITHOUT_WORKFLOW_SCOPE = (
    "github.com\n"
    "  Logged in to github.com account octocat (keyring)\n"
    "  - Active account: true\n"
    "  - Git operations protocol: https\n"
    "  - Token: ghp_************************************\n"
    "  - Token scopes: 'gist', 'read:org', 'repo'\n"
)

_GH_STATUS_SCOPES_NONE = (
    "github.com\n"
    "  Logged in to github.com account octocat (keyring)\n"
    "  - Active account: true\n"
    "  - Token: github_pat_**************\n"
    "  - Token scopes: none\n"
)

_GH_STATUS_NO_SCOPES_LINE = "github.com\n  Logged in to github.com account octocat (keyring)\n"


class _FakeGhWebLoginProcess:
    """Replays the real gh 2.95 web-login PTY transcript with pexpect-like semantics.

    Behavioral model, each aspect observed against the real gh inside a
    minds container:

    - The one-time code and verification URL are emitted before any prompt
      (`_REAL_GH_WEB_TTY_TRANSCRIPT`).
    - Input sent before gh has produced a match (i.e. during the startup
      terminal-query handshake) is swallowed and never satisfies the "Press
      Enter" prompt; it is recorded in `swallowed_input`.
    - A newline sent once the prompt is on screen advances gh: it attempts
      (and, headless, fails) to open a browser, polls for the authorization,
      and exits -- modeled as `_REAL_GH_WEB_POST_ENTER_OUTPUT` followed by
      EOF.

    `initial_stream` overrides the transcript so a test can simulate gh
    hanging before the code appears.
    """

    def __init__(self, initial_stream: str = _REAL_GH_WEB_TTY_TRANSCRIPT) -> None:
        self._stream = initial_stream
        self._offset = 0
        self._has_matched = False
        self._exited = False
        self.swallowed_input: list[str] = []
        self.prompt_answers: list[str] = []
        self.before: Any = None
        self.after: Any = None
        self.timeout: float = 30.0
        self.terminate_calls = 0
        self.close_calls = 0

    def expect(self, patterns: list[Any]) -> int:
        remaining = self._stream[self._offset :]
        best_match: re.Match[str] | None = None
        best_index: int | None = None
        for index, pattern in enumerate(patterns):
            if not isinstance(pattern, re.Pattern):
                continue
            match = pattern.search(remaining)
            if match is not None and (best_match is None or match.start() < best_match.start()):
                best_match = match
                best_index = index
        if best_match is not None and best_index is not None:
            self.before = remaining[: best_match.start()]
            self.after = best_match.group(0)
            self._offset += best_match.end()
            self._has_matched = True
            return best_index
        if self._exited and pexpect.EOF in patterns:
            self.before = remaining
            self.after = pexpect.EOF
            self._offset = len(self._stream)
            return patterns.index(pexpect.EOF)
        return patterns.index(pexpect.TIMEOUT)

    def sendline(self, line: str = "") -> None:
        # Real gh ignores stdin once it is polling / has exited.
        if self._exited:
            return
        # During the terminal-query handshake (nothing matched yet), gh reads
        # and discards stdin, so the input never reaches the prompt.
        if not self._has_matched:
            self.swallowed_input.append(line)
            return
        self.prompt_answers.append(line)
        self._stream += _REAL_GH_WEB_POST_ENTER_OUTPUT
        self._exited = True

    def isalive(self) -> bool:
        return not self._exited

    def terminate(self, force: bool = False) -> None:
        self.terminate_calls += 1
        self._exited = True

    def close(self) -> None:
        self.close_calls += 1


class _RecordingSpawner:
    """A `pexpect_spawner` fake that records the spawn arguments."""

    def __init__(self, process: _FakeGhWebLoginProcess) -> None:
        self._process = process
        self.spawn_calls: list[tuple[str, list[str], float]] = []

    def __call__(self, executable: str, args: list[str], timeout: float) -> _FakeGhWebLoginProcess:
        self.spawn_calls.append((executable, list(args), timeout))
        return self._process


class _CannedCommandRunner:
    """A `command_runner` fake serving canned `gh auth status` / `setup-git` results."""

    def __init__(self, status_output: str) -> None:
        self._status_output = status_output
        self.argvs: list[tuple[str, ...]] = []

    def __call__(
        self,
        command: list[str],
        timeout: float,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
    ) -> FakeFinishedProcess:
        self.argvs.append(tuple(command))
        if command[:3] == ["gh", "auth", "status"]:
            # gh auth status writes its report to stderr.
            return FakeFinishedProcess(stderr=self._status_output, returncode=0)
        return FakeFinishedProcess(returncode=0)


@pytest.mark.parametrize("token_var", _GH_TOKEN_ENV_VARS)
def test_gh_child_env_strips_each_github_token_var(token_var: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each GitHub token variable is removed from the child environment."""
    monkeypatch.setenv(token_var, "ghp_shadowing_value")
    child_env = _gh_child_env()
    assert token_var not in child_env


def test_gh_child_env_preserves_unrelated_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-token variables survive the scrub so gh keeps its normal environment."""
    monkeypatch.setenv("GH_TOKEN", "ghp_shadowing_value")
    monkeypatch.setenv("PATH_MARKER_FOR_TEST", "keep-me")
    child_env = _gh_child_env()
    assert child_env["PATH_MARKER_FOR_TEST"] == "keep-me"
    assert "GH_TOKEN" not in child_env


def test_gh_child_env_does_not_mutate_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrubbing is child-only: os.environ still holds the token afterwards."""
    monkeypatch.setenv("GH_TOKEN", "ghp_shadowing_value")
    _gh_child_env()
    assert os.environ["GH_TOKEN"] == "ghp_shadowing_value"


def test_gh_child_env_overrides_apply_after_scrub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller overrides are layered onto the scrubbed base environment."""
    monkeypatch.setenv("GH_TOKEN", "ghp_shadowing_value")
    child_env = _gh_child_env({"EXTRA_VAR_FOR_TEST": "value"})
    assert child_env["EXTRA_VAR_FOR_TEST"] == "value"
    assert "GH_TOKEN" not in child_env


def test_start_web_login_captures_code_and_url_from_real_transcript() -> None:
    """The code and URL are extracted from the real gh 2.95 PTY transcript.

    gh prints them BEFORE any prompt, wrapped in ANSI styling and preceded by
    a terminal-query handshake; the service must pull both out without any
    prompt choreography.
    """
    fake_process = _FakeGhWebLoginProcess()
    spawner = _RecordingSpawner(fake_process)
    service = GitHubAuthService(pexpect_spawner=spawner)
    result = service.start_web_login()
    assert result.user_code == _REAL_USER_CODE
    assert result.verification_url == _REAL_VERIFICATION_URL
    assert result.session_id


def test_start_web_login_sends_nothing_until_code_is_captured() -> None:
    """No stdin is written before the code/URL appear; the prompt is answered after.

    gh's terminal-query handshake swallows early input, so a preemptive
    newline never satisfies the "Press Enter" prompt and gh never polls.
    The regression pinned: zero swallowed input, exactly one post-capture
    prompt answer.
    """
    fake_process = _FakeGhWebLoginProcess()
    service = GitHubAuthService(pexpect_spawner=_RecordingSpawner(fake_process))
    service.start_web_login()
    assert fake_process.swallowed_input == []
    assert fake_process.prompt_answers == [""]


def test_web_login_requests_workflow_scope() -> None:
    """The web login passes --scopes workflow so pushes touching .github/workflows work.

    gh's default web-login scopes (repo, read:org, gist) do NOT include
    `workflow`, and GitHub rejects pushes that modify workflow files without
    it -- the mind's repo ships CI workflows, so the credential must carry it.
    """
    fake_process = _FakeGhWebLoginProcess()
    spawner = _RecordingSpawner(fake_process)
    service = GitHubAuthService(pexpect_spawner=spawner)
    service.start_web_login()
    (_, args, _) = spawner.spawn_calls[0]
    scopes_flag_index = args.index("--scopes")
    assert args[scopes_flag_index + 1] == "workflow"


def test_submit_code_completes_after_prompt_answered() -> None:
    """Completing the flow reaches EOF because the prompt answer let gh poll.

    After the code/URL capture the service answered the "Press Enter" prompt,
    so gh proceeded (browser-open failure is harmless) and exited once the
    user authorized; `submit_code` then wires git and reports the status.
    """
    fake_process = _FakeGhWebLoginProcess()
    runner = _CannedCommandRunner(_GH_STATUS_WITH_WORKFLOW_SCOPE)
    service = GitHubAuthService(command_runner=runner, pexpect_spawner=_RecordingSpawner(fake_process))
    start = service.start_web_login()
    status = service.submit_code(start.session_id)
    assert status.logged_in is True
    assert status.username == "octocat"
    assert ("gh", "auth", "setup-git", "--hostname", "github.com") in runner.argvs


def test_start_web_login_times_out_when_code_never_appears() -> None:
    """A gh that never prints the code raises a timeout error and is cleaned up."""
    fake_process = _FakeGhWebLoginProcess(initial_stream="\x1b]11;?\x1b\\\x1b[6n")
    service = GitHubAuthService(pexpect_spawner=_RecordingSpawner(fake_process))
    with pytest.raises(GitHubAuthError, match="Timed out waiting for the one-time device code"):
        service.start_web_login()
    assert fake_process.terminate_calls >= 1
    assert fake_process.close_calls >= 1


def test_get_auth_status_parses_token_scopes() -> None:
    """The quoted scope list from gh auth status parses into a tuple, no warning."""
    runner = _CannedCommandRunner(_GH_STATUS_WITH_WORKFLOW_SCOPE)
    service = GitHubAuthService(command_runner=runner)
    status = service.get_auth_status()
    assert status.logged_in is True
    assert status.token_scopes == ("gist", "read:org", "repo", "workflow")
    assert status.warning is None


def test_get_auth_status_warns_when_workflow_scope_missing() -> None:
    """A classic token without the workflow scope surfaces a clear warning.

    The PAT path cannot add scopes to a pasted token, so the status is where
    the user learns their token cannot push .github/workflows changes.
    """
    runner = _CannedCommandRunner(_GH_STATUS_WITHOUT_WORKFLOW_SCOPE)
    service = GitHubAuthService(command_runner=runner)
    status = service.get_auth_status()
    assert status.logged_in is True
    assert status.token_scopes == ("gist", "read:org", "repo")
    assert status.warning is not None
    assert "workflow" in status.warning


def test_get_auth_status_does_not_warn_for_scopeless_token() -> None:
    """A "Token scopes: none" report (fine-grained PAT) parses to an empty tuple, no warning.

    Fine-grained PATs carry no classic scopes; their workflow permission is
    invisible to gh auth status, so warning would be a guess.
    """
    runner = _CannedCommandRunner(_GH_STATUS_SCOPES_NONE)
    service = GitHubAuthService(command_runner=runner)
    status = service.get_auth_status()
    assert status.logged_in is True
    assert status.token_scopes == ()
    assert status.warning is None


def test_get_auth_status_handles_missing_scopes_line() -> None:
    """Output without a Token scopes line leaves scopes unknown and no warning."""
    runner = _CannedCommandRunner(_GH_STATUS_NO_SCOPES_LINE)
    service = GitHubAuthService(command_runner=runner)
    status = service.get_auth_status()
    assert status.logged_in is True
    assert status.token_scopes is None
    assert status.warning is None
