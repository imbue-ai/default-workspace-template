"""A visiting agent end to end: the served helper joins through the gateway and works through it.

The gateway app is served on loopback (no caddy, no relay), and the helper,
run under node exactly as an agent would run it, joins with a minted code,
runs a command, reads and writes files with nothing but its admission token,
and is shut out the moment the grant is revoked. The share's HTTPS origins do
not exist here, which the helper never notices: it keeps talking to the
address it joined at.
"""

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from share_gateway.agent_access import AgentAccessPaths
from share_gateway.agent_access import LastSeenStore
from share_gateway.agent_access import mint_invite
from share_gateway.agent_access import revoke
from share_gateway.agent_orientation import HELPER_PATH
from share_gateway.agent_routes import AgentAccessContext
from share_gateway.agent_workspace import WorkspaceCoordinates
from share_gateway.caddyfile import RegisteredApp
from share_gateway.request_handler import SilentRequestHandler
from share_gateway.testing import build_agent_test_app
from share_gateway.testing import make_workspace_repo
from share_gateway.testing import serve_wsgi

_DOMAIN = "alpha.7f3a.us1.example.test"
_AUTH_LABEL = "auth-e2ee2ee2"
_HOST_ID = "host-" + "e" * 32
_PRIMER = "# {{workspace_name}}\n{{skills_table}}\n{{apps_table}}\n{{connected_accounts}}\n{{changes}}\n"
_INVITE = "Join {{auth_origin}} with {{code}}\n"
_EXIT_REVOKED = 77
_EXIT_USAGE = 64


def _require_node() -> None:
    if shutil.which("node") is None:
        pytest.skip("the visiting-agent end-to-end test needs node")


@contextmanager
def _gateway(tmp_path: Path, repo: Path, paths: AgentAccessPaths) -> Generator[str, None, None]:
    apps = [
        RegisteredApp("system_interface", "system_interface-sh000000", "127.0.0.1", 8000, is_grantable=False),
        RegisteredApp("chat", "chat-ch000000", "127.0.0.1", 8010, is_grantable=True),
    ]
    share_grants = tmp_path / "share_grants.toml"
    share_grants.write_text("[workspace]\nusers = []\nemails = []\nemail_domains = []\n")
    context = AgentAccessContext(
        paths=paths,
        coordinates=WorkspaceCoordinates(
            host_id=_HOST_ID,
            workspace_id="agent-" + "f" * 32,
            display_name="E2E Lab",
        ),
        repo_root=repo,
        environ={},
        last_seen=LastSeenStore(tmp_path / "state" / "last_seen.json"),
        skills_cache_path=tmp_path / "state" / "skills_index.json",
        now=lambda: datetime.now(timezone.utc),
    )
    app = build_agent_test_app(
        context, apps, share_grants, _DOMAIN, _AUTH_LABEL, "e2e-signing-secret-long-enough-for-hs256-use"
    )
    # Served as the runner serves it, so a command's stream goes out in chunks here as it does in a workspace.
    with serve_wsgi(app, request_handler=SilentRequestHandler, is_threaded=True) as origin:
        yield origin


def _wsvisit(
    state_dir: Path, arguments: Sequence[str], stdin_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["node", str(HELPER_PATH), *arguments],
        input=stdin_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
        env={**os.environ, "WSVISIT_STATE_DIR": str(state_dir)},
    )


@pytest.mark.timeout(60)
def test_a_visiting_agent_joins_runs_reads_writes_and_is_shut_out_on_revoke(tmp_path: Path) -> None:
    _require_node()
    repo = make_workspace_repo(tmp_path, _PRIMER, _INVITE)
    (repo / "data").mkdir()
    paths = AgentAccessPaths(
        grants_path=tmp_path / "agent_access" / "grants.toml",
        invites_path=tmp_path / "agent_access" / "invites.toml",
        events_dir=tmp_path / "events",
    )
    state_dir = tmp_path / "agent_state"
    state_dir.mkdir(mode=0o700)

    with _gateway(tmp_path, repo, paths) as gateway_origin:
        minted = mint_invite(paths, "workspace", "user:owner", "", None, datetime.now(timezone.utc), via="test")

        joined = _wsvisit(state_dir, ["join", gateway_origin, minted.code, "--name", "e2e agent"])
        assert joined.returncode == 0, joined.stderr.decode()
        assert "# E2E Lab" in joined.stdout.decode()
        assert "build-app" in joined.stdout.decode()
        assert "joined E2E Lab as e2e agent" in joined.stderr.decode()
        # The helper is the copy this gateway publishes, so it has nothing to say about reinstalling.
        assert "not the one the workspace publishes" not in joined.stderr.decode()
        config = json.loads((state_dir / _DOMAIN / "config.json").read_text())

        ran = _wsvisit(state_dir, ["run", "--", "sh", "-c", "echo hello from $PWD; echo trouble >&2; exit 3"])
        assert ran.returncode == 3, ran.stderr.decode()
        assert ran.stdout.decode() == f"hello from {repo}\n"
        assert ran.stderr.decode() == "trouble\n"
        elsewhere = _wsvisit(state_dir, ["run", "--cwd", "data", "--", "pwd"])
        assert elsewhere.returncode == 0 and elsewhere.stdout.decode() == f"{repo / 'data'}\n"
        out_of_time = _wsvisit(state_dir, ["run", "--timeout", "1", "--", "sleep", "600"])
        assert out_of_time.returncode == 1
        assert out_of_time.stderr.decode() == "wsvisit: the command ran out of time and was stopped\n"
        not_started = _wsvisit(state_dir, ["run", "--", "no-such-command-5d0e"])
        assert not_started.returncode == 1
        assert "the command could not be started" in not_started.stderr.decode()
        assert "no-such-command-5d0e" in not_started.stderr.decode()

        source = tmp_path / "note.txt"
        source.write_bytes(b"a note from outside\n")
        written = _wsvisit(state_dir, ["write", "data/note.txt", "--from", str(source), "--mode", "640"])
        assert written.returncode == 0, written.stderr.decode()
        assert (repo / "data" / "note.txt").read_bytes() == b"a note from outside\n"
        assert stat.S_IMODE((repo / "data" / "note.txt").stat().st_mode) == 0o640
        from_stdin = _wsvisit(state_dir, ["write", "data/piped.bin"], stdin_bytes=b"\x00piped\xff")
        assert from_stdin.returncode == 0, from_stdin.stderr.decode()
        read = _wsvisit(state_dir, ["read", "data/piped.bin"])
        assert read.returncode == 0, read.stderr.decode()
        assert read.stdout == b"\x00piped\xff"
        missing = _wsvisit(state_dir, ["read", "data/absent.txt"])
        assert missing.returncode == _EXIT_USAGE, missing.stderr.decode()
        assert "no such file" in missing.stderr.decode()
        a_directory = _wsvisit(state_dir, ["read", "data"])
        assert a_directory.returncode == _EXIT_USAGE
        assert a_directory.stderr.decode() == (
            "wsvisit: read answered 400: could not read data: it is not a regular file\n"
        )

        me = _wsvisit(state_dir, ["me"])
        assert me.returncode == 0, me.stderr.decode()
        assert json.loads(me.stdout.decode())["name"] == "e2e agent"
        assert config["agent_id"] in me.stdout.decode()

        revoke(paths, config["agent_id"], "user:owner", True, datetime.now(timezone.utc), via="test")
        for arguments in (["run", "--", "echo", "still here?"], ["read", "data/note.txt"]):
            shut_out = _wsvisit(state_dir, arguments)
            assert shut_out.returncode == _EXIT_REVOKED, shut_out.stderr.decode()
            assert "revoked" in shut_out.stderr.decode()
            assert shut_out.stdout == b""
