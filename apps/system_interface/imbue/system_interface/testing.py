"""Shared test fakes for the system_interface package.

Houses deterministic stand-ins for outside-world dependencies that
`ClaudeAuthService` takes as constructor-injected callables
(`command_runner`, `pexpect_spawner`). Both `claude_auth_test.py` and
`claude_auth_endpoints_test.py` need the same fakes, so they live here
rather than being copy-pasted into each test module.
"""

from __future__ import annotations

import re
from typing import Sequence

import click

from imbue.mngr.main import cli


class MngrArgvContractError(AssertionError):
    """Raised when an argv is not accepted by the live mngr CLI surface."""


def assert_mngr_argv_valid(argv: Sequence[str]) -> None:
    """Assert that ``argv`` is structurally accepted by the live mngr CLI.

    Resolves the (possibly nested) subcommand against ``imbue.mngr.main.cli``
    and parses the remaining tokens with each command's low-level option parser,
    so value validators (``Path(exists=True)``, callbacks, type coercion,
    required-option enforcement) do NOT run -- we verify the CLI *surface* the
    code depends on, not the runtime values a given invocation carries.
    ``argv[0]`` (the mngr binary, possibly an absolute path) is ignored.

    This is a copy of the repo-root ``mngr_cli_contract.assert_mngr_argv_valid``;
    apps/system_interface runs as an isolated package (its own venv + pytest
    invocation) and cannot import repo-root test modules, so the validator is
    duplicated here. Both consume the same live ``imbue.mngr.main.cli``.
    """
    try:
        _resolve_against_cli(cli, click.Context(cli, info_name="mngr"), list(argv[1:]))
    except click.exceptions.ClickException as exc:
        raise MngrArgvContractError(
            f"mngr argv not accepted by the live CLI: {list(argv)!r}\n"
            f"  {type(exc).__name__}: {exc.format_message()}\n"
            f"  The vendored mngr CLI surface changed under this invocation. "
            f"Update the producing code to match the current mngr CLI."
        ) from exc


def _resolve_against_cli(
    command: click.Command, ctx: click.Context, tokens: list[str]
) -> None:
    """Descend the click tree for ``tokens``, raising on an unknown subcommand
    or option. Recurses through nested groups (mngr's tree is shallow); a leaf
    command's low-level parser validates the option tokens without running
    click's value converters."""
    if isinstance(command, click.Group):
        name, subcommand, rest = command.resolve_command(ctx, tokens)
        if subcommand is None:
            raise click.exceptions.UsageError(f"No such command {name!r}.")
        _resolve_against_cli(
            subcommand, click.Context(subcommand, info_name=name, parent=ctx), rest
        )
    else:
        command.make_parser(ctx).parse_args(args=list(tokens))


class FakeFinishedProcess:
    """Minimal stand-in for a `FinishedProcess` returned by `command_runner`.

    The real subprocess runner produces an object with `stdout`, `stderr`,
    and `returncode`; this class exposes just those three so tests can
    drive every branch the `claude_auth` callers care about.
    """

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakePexpectProcess:
    """Records the inputs the OAuth flow sends to a `pexpect.spawn`.

    Constructor arguments parameterize how the fake responds to `expect()`:

    - `url_match`: when non-None, the first `expect()` returns
      `expect_return_index` (default 0 for the URL-matched branch) and
      `self.match` is preset to the result of regex-matching `url_match`.
      When None, the first `expect()` returns `expect_return_index`
      (typically 1 for EOF or 2 for TIMEOUT) without setting `match`.
    - `expect_return_index`: index returned on the first `expect()` call.
      Lets a test simulate the URL-found / EOF-before-URL / timeout
      branches of `_spawn_oauth_and_parse_url`.
    - `eof_return_index`: index returned on every subsequent `expect()`
      call. Defaults to 0 (the EOF branch in `_drive_oauth_code`'s
      `[pexpect.EOF, pexpect.TIMEOUT]` pattern) so the post-code-submit
      teardown lands in the success path.
    """

    def __init__(
        self,
        url_match: str | None = None,
        expect_return_index: int = 0,
        eof_return_index: int = 0,
    ) -> None:
        self._expect_return_index = expect_return_index
        self._eof_return_index = eof_return_index
        self._expect_call_count = 0
        self.sendline_calls: list[str] = []
        self.terminate_calls = 0
        self.close_calls = 0
        self.timeout: float | None = None
        self.match: re.Match[str] | None = None
        if url_match is not None:
            self.match = re.compile(r".*").match(url_match)
            assert self.match is not None

    def expect(self, _patterns: object) -> int:
        self._expect_call_count += 1
        if self._expect_call_count == 1:
            return self._expect_return_index
        return self._eof_return_index

    def sendline(self, s: str) -> None:
        self.sendline_calls.append(s)

    def isalive(self) -> bool:
        return True

    def terminate(self, force: bool = False) -> None:
        self.terminate_calls += 1

    def close(self) -> None:
        self.close_calls += 1
