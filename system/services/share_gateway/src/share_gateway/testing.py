"""Test utilities shared across the gateway's test modules."""

import json
import subprocess
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import PublicFormat
from flask import Flask
from werkzeug.serving import make_server
from werkzeug.wrappers import Request
from werkzeug.wrappers import Response

from share_gateway.agent_routes import AgentAccessContext
from share_gateway.agent_routes import register_agent_routes
from share_gateway.caddyfile import RegisteredApp
from share_gateway.handoff import JwksCache
from share_gateway.handoff import SingleUseJtiRegistry
from share_gateway.materials import ShareMaterials
from share_gateway.runner import build_agent_admitter
from share_gateway.server import PendingLoginRegistry
from share_gateway.server import build_gateway_app


def set_cookies_by_name(response: Response) -> dict[str, str]:
    """The response's rendered ``Set-Cookie`` headers, keyed by cookie name."""
    return {header.split("=", 1)[0]: header for header in response.headers.getlist("Set-Cookie")}


@contextmanager
def serve_json(body: object) -> Generator[str, None, None]:
    """Serve ``body`` as JSON at every path of a loopback server; yields the server's URL."""

    @Request.application
    def app(request: Request) -> Response:
        return Response(json.dumps(body), mimetype="application/json")

    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/share/jwks.json"
    finally:
        server.shutdown()
        thread.join()


def _git_environ(repo: Path) -> dict[str, str]:
    """A fixed committer identity and a HOME inside the repo, so no user config leaks into the test history."""
    return {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "HOME": str(repo),
    }


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, env=_git_environ(repo), check=True, stdout=subprocess.DEVNULL)


def git_output(repo: Path, *args: str) -> str:
    """git's stdout in ``repo``, stripped."""
    return subprocess.run(
        ["git", *args], cwd=repo, env=_git_environ(repo), check=True, stdout=subprocess.PIPE, text=True
    ).stdout.strip()


def make_workspace_repo(root: Path, primer_template: str, invite_template: str) -> Path:
    """A small committed workspace checkout: two skills and the two visiting-agent reference files."""
    repo = root / "workspace"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    build_app = repo / ".agents" / "skills" / "build-app"
    build_app.mkdir(parents=True)
    (build_app / "SKILL.md").write_text('---\nname: build-app\ndescription: "Build an app, in quotes"\n---\n# Build\n')
    (build_app / "scripts").mkdir()
    (build_app / "scripts" / "run.py").write_text("print('hi')\n")
    notify = repo / ".agents" / "skills" / "notify-user"
    notify.mkdir()
    (notify / "SKILL.md").write_text("---\nname: notify-user\ndescription: Tell the user.\n---\n")
    references = repo / ".agents" / "shared" / "references"
    references.mkdir(parents=True)
    (references / "visiting-agents.md").write_text(primer_template)
    (references / "visiting-agents-invite.md").write_text(invite_template)
    commit_all(repo, "Initial workspace commit")
    return repo


def commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)


def new_ed25519_public_key_line(comment: str = "") -> str:
    """A fresh Ed25519 OpenSSH public key line, the shape an agent presents at join, with an optional comment."""
    line = (
        Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii")
    )
    return f"{line} {comment}" if comment else line


def build_agent_test_app(
    context: AgentAccessContext,
    apps: list[RegisteredApp],
    share_grants_path: Path,
    workspace_domain: str,
    auth_label: str,
    signing_secret: str,
) -> Flask:
    """The gateway app with bearer admission and the /_agent/* routes wired to ``context``, as the runner builds it."""
    materials = ShareMaterials(
        workspace_domain=workspace_domain,
        relay_token="tok",
        connector_url=f"https://connector.{workspace_domain}",
        broker_url=f"https://accounts.{workspace_domain}",
        chrome_origin="",
    )
    app = build_gateway_app(
        materials=materials,
        grants_path=share_grants_path,
        signing_secret=signing_secret,
        jwks_cache=JwksCache(f"https://accounts.{workspace_domain}/share/jwks.json", preloaded_keys_by_kid={}),
        jti_registry=SingleUseJtiRegistry(),
        pending_logins=PendingLoginRegistry(),
        auth_label=auth_label,
        get_registered_apps=lambda: apps,
        admit_agent=build_agent_admitter(context, lambda: list(apps)),
    )
    register_agent_routes(
        app,
        context=context,
        workspace_domain=workspace_domain,
        auth_label=auth_label,
        signing_secret=signing_secret,
        share_grants_path=share_grants_path,
        get_registered_apps=lambda: apps,
    )
    return app
