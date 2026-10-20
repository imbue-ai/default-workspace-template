"""A visiting agent end to end: the served helper joins through the gateway and works through a real owner-exec.

The gateway app is served on loopback (no caddy, no relay), the pinned
owner-exec binary runs in its inner role against a temporary
``authorized_keys`` and host key, and the helper (run under node, exactly as an
agent would run it) joins with a minted code, signs ``run``, ``read``, and
``write`` requests the daemon verifies, verifies the daemon's signed responses
and stream trailer against the pinned host key, and is shut out the moment the
grant is revoked. The share's HTTPS origins do not exist here, so after the join
the test points the helper's stored exec origin at the daemon's loopback
address, which is the one thing a real share would resolve for it.
"""

import json
import os
import shutil
import socket
import subprocess
import threading
from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path

import httpx
import pytest
from share_gateway.agent_access import AgentAccessPaths
from share_gateway.agent_access import LastSeenStore
from share_gateway.agent_access import mint_invite
from share_gateway.agent_access import revoke
from share_gateway.agent_orientation import HELPER_PATH
from share_gateway.agent_routes import AgentAccessContext
from share_gateway.agent_workspace import WorkspaceCoordinates
from share_gateway.caddyfile import RegisteredApp
from share_gateway.testing import build_agent_test_app
from share_gateway.testing import make_workspace_repo
from werkzeug.serving import make_server

_DOMAIN = "alpha.7f3a.us1.example.test"
_AUTH_LABEL = "auth-e2ee2ee2"
_HOST_ID = "host-" + "e" * 32
_PRIMER = "# {{workspace_name}}\n{{skills_table}}\n{{apps_table}}\n{{connected_accounts}}\n{{changes}}\n"
_INVITE = "Join {{auth_origin}} with {{code}}\n"
_EXIT_REVOKED = 77


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _owner_exec_binary() -> str | None:
    configured = os.environ.get("OWNER_EXEC_BINARY", "")
    if configured and Path(configured).exists():
        return configured
    return shutil.which("owner-exec")


def _require_prerequisites() -> str:
    binary = _owner_exec_binary()
    present_by_name = {"node": shutil.which("node"), "ssh-keygen": shutil.which("ssh-keygen"), "owner-exec": binary}
    missing = [name for name, present in present_by_name.items() if not present]
    if missing:
        pytest.skip(
            f"the visiting-agent end-to-end test needs {', '.join(missing)} (set OWNER_EXEC_BINARY to a built daemon)"
        )
    assert binary is not None
    return binary


def _wait_for_listening(process: subprocess.Popen[bytes]) -> None:
    """Block until the daemon reports it is listening (its first stderr line), or it exits."""
    assert process.stderr is not None
    for line in iter(process.stderr.readline, b""):
        if b"listening on" in line:
            return
    raise AssertionError(f"owner-exec exited before listening (rc={process.wait()})")


@contextmanager
def _owner_exec(
    binary: str, repo: Path, authorized_keys: Path, host_key: Path, port: int
) -> Generator[str, None, None]:
    config = repo.parent / "owner_exec.toml"
    config.write_text(
        "\n".join(
            [
                'role = "inner"',
                f'host_id = "{_HOST_ID}"',
                'listen_host = "127.0.0.1"',
                f"listen_port = {port}",
                f'repo_root = "{repo}"',
                f'authorized_keys_path = "{authorized_keys}"',
                f'host_key_path = "{host_key}"',
                "grants_enabled = false",
                'share_env_path = ""',
                "register_port = false",
            ]
        )
        + "\n"
    )
    process = subprocess.Popen([binary, "--config", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _wait_for_listening(process)
        assert httpx.get(f"http://127.0.0.1:{port}/_alive", timeout=10.0).status_code == 200
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        process.wait(timeout=10)


@contextmanager
def _gateway(
    tmp_path: Path, repo: Path, paths: AgentAccessPaths, host_public_key: str, exec_label: str
) -> Generator[str, None, None]:
    apps = [
        RegisteredApp("system_interface", "system_interface-sh000000", "127.0.0.1", 8000, is_grantable=False),
        RegisteredApp("owner-exec", exec_label, "127.0.0.1", 8793, is_grantable=False),
        RegisteredApp("chat", "chat-ch000000", "127.0.0.1", 8010, is_grantable=True),
    ]
    share_grants = tmp_path / "share_grants.toml"
    share_grants.write_text("[workspace]\nusers = []\nemails = []\nemail_domains = []\n")
    context = AgentAccessContext(
        paths=paths,
        coordinates=WorkspaceCoordinates(
            host_id=_HOST_ID, workspace_id="agent-" + "f" * 32, display_name="E2E Lab", host_public_key=host_public_key
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
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, name="e2e-gateway", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=10)


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


def test_a_visiting_agent_joins_runs_reads_writes_and_is_shut_out_on_revoke(tmp_path: Path) -> None:
    binary = _require_prerequisites()
    repo = make_workspace_repo(tmp_path, _PRIMER, _INVITE)
    (repo / "data").mkdir()
    authorized_keys = tmp_path / "ssh" / "authorized_keys"
    host_key = tmp_path / "host_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)], check=True)
    host_public_key = host_key.with_suffix(".pub").read_text().strip()
    paths = AgentAccessPaths(
        grants_path=tmp_path / "agent_access" / "grants.toml",
        invites_path=tmp_path / "agent_access" / "invites.toml",
        authorized_keys_path=authorized_keys,
        events_dir=tmp_path / "events",
    )
    state_dir = tmp_path / "agent_state"
    state_dir.mkdir(mode=0o700)
    exec_port = _free_port()

    with _owner_exec(binary, repo, authorized_keys, host_key, exec_port) as exec_origin:
        with _gateway(tmp_path, repo, paths, host_public_key, "owner-exec-ex000000") as gateway_origin:
            minted = mint_invite(
                paths,
                "workspace",
                "user:owner",
                "",
                None,
                datetime.now(timezone.utc),
                via="test",
            )

            joined = _wsvisit(state_dir, ["join", gateway_origin, minted.code, "--name", "e2e agent"])
            assert joined.returncode == 0, joined.stderr.decode()
            assert "# E2E Lab" in joined.stdout.decode()
            assert "build-app" in joined.stdout.decode()
            assert "joined E2E Lab as e2e agent" in joined.stderr.decode()

            # The share's HTTPS origins do not exist here: point the stored exec
            # origin at the daemon's loopback address, as a real share would resolve it.
            config_path = state_dir / _DOMAIN / "config.json"
            config = json.loads(config_path.read_text())
            assert config["exec"]["audience"] == f"container:{_HOST_ID}"
            assert config["exec"]["host_public_key"] == host_public_key
            config["exec"]["origin"] = exec_origin
            config_path.write_text(json.dumps(config))

            ran = _wsvisit(state_dir, ["run", "--", "sh", "-c", "echo hello from $PWD; echo trouble >&2; exit 3"])
            assert ran.returncode == 3, ran.stderr.decode()
            assert ran.stdout.decode() == f"hello from {repo}\n"
            assert "trouble" in ran.stderr.decode()

            source = tmp_path / "note.txt"
            source.write_bytes(b"a note from outside\n")
            written = _wsvisit(state_dir, ["write", "data/note.txt", "--from", str(source)])
            assert written.returncode == 0, written.stderr.decode()
            assert (repo / "data" / "note.txt").read_bytes() == b"a note from outside\n"
            read = _wsvisit(state_dir, ["read", "data/note.txt"])
            assert read.returncode == 0, read.stderr.decode()
            assert read.stdout == b"a note from outside\n"
            missing = _wsvisit(state_dir, ["read", "data/absent.txt"])
            assert missing.returncode == 64, missing.stderr.decode()
            assert "no such file" in missing.stderr.decode()

            me = _wsvisit(state_dir, ["me"])
            assert me.returncode == 0, me.stderr.decode()
            assert json.loads(me.stdout.decode())["name"] == "e2e agent"
            assert config["agent_id"] in me.stdout.decode()

            # The owner revokes the grant; the daemon refuses the key on the next request and
            # the helper learns from the gateway that the grant is gone.
            revoke(
                paths,
                config["agent_id"],
                "user:owner",
                True,
                datetime.now(timezone.utc),
                via="test",
            )
            assert authorized_keys.read_text() == ""
            shut_out = _wsvisit(state_dir, ["run", "--", "echo", "still here?"])
            assert shut_out.returncode == _EXIT_REVOKED, shut_out.stderr.decode()
            assert "revoked" in shut_out.stderr.decode()
