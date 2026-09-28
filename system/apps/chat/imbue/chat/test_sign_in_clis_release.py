"""The sign-in CLIs the workspace image pins still open a loopback callback we can relay.

Run inside the workspace image, where `claude` and `codex` are the pinned versions
(`system/scripts/setup_system.sh`). Each test starts a real sign-in with the browser shim as
`$BROWSER`, checks the page it hands over calls back to a loopback listener that is really up, and
kills the CLI before anything is authorized: no account is ever signed in, and nothing reaches a
provider beyond the CLI fetching its own configuration.
"""

from __future__ import annotations

import os
import socket
import tempfile
from collections.abc import Iterator
from contextlib import closing
from contextlib import contextmanager
from pathlib import Path

import pytest

from imbue.chat.harnesses.codex.sign_in import app_server_argv
from imbue.chat.harnesses.codex.sign_in import connect_login_client
from imbue.chat.harnesses.pty_auth import drain_pty_stream
from imbue.chat.harnesses.pty_auth import safe_close
from imbue.chat.harnesses.pty_auth import safe_terminate
from imbue.chat.harnesses.pty_auth import spawn_pty
from imbue.chat.harnesses.sign_in_relay import parse_relay_target
from imbue.chat.harnesses.sign_in_relay import read_sign_in_url

_SHIM = Path(__file__).resolve().parents[5] / "system" / "scripts" / "minds_browser_shim"
_START_SECONDS = 60.0


def _is_listening(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.settimeout(2.0)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@contextmanager
def _running(binary: str, args: list[str], env: dict[str, str]) -> Iterator[object]:
    process = spawn_pty(binary, args, _START_SECONDS, env={**os.environ, **env})
    try:
        yield process
    finally:
        safe_terminate(process)
        safe_close(process)


@pytest.mark.release
@pytest.mark.parametrize("mode", ["--claudeai", "--console"])
def test_claude_hands_its_browser_a_page_that_calls_back_to_a_live_loopback_listener(
    tmp_path: Path, mode: str
) -> None:
    url_file = tmp_path / "relay_url"
    env = {
        "CLAUDE_CONFIG_DIR": str(tmp_path / "config"),
        "BROWSER": str(_SHIM),
        "MINDS_SIGNIN_URL_FILE": str(url_file),
    }
    with _running("claude", ["auth", "login", mode], env) as process:
        drain_pty_stream(
            process, "", lambda _: read_sign_in_url(url_file) is not None, deadline_seconds=_START_SECONDS
        )
        url = read_sign_in_url(url_file)
        assert url is not None, "claude never ran $BROWSER"
        target = parse_relay_target(url)
        assert target is not None, f"claude's page does not call back to a loopback listener: {url}"
        assert _is_listening(target.port)


@pytest.mark.release
def test_codexs_browser_login_calls_back_to_a_live_loopback_listener(tmp_path: Path) -> None:
    # A unix socket path must stay short, which a pytest tmp_path does not.
    with tempfile.TemporaryDirectory(prefix="cx-") as short_dir:
        socket_path = Path(short_dir) / "codex.sock"
        env = {"CODEX_HOME": str(tmp_path / "codex"), "BROWSER": str(_SHIM)}
        (tmp_path / "codex").mkdir()
        with _running("codex", app_server_argv(socket_path), env) as process:
            drain_pty_stream(process, "", lambda _: socket_path.exists(), deadline_seconds=_START_SECONDS)
            client = connect_login_client(socket_path)
            try:
                login = client.start_chatgpt_login()
                target = parse_relay_target(login.auth_url)
                assert target is not None, f"codex's page does not call back to a loopback listener: {login.auth_url}"
                assert _is_listening(target.port)
            finally:
                client.close()
