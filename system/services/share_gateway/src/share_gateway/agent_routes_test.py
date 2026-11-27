import base64
import hashlib
import json
import stat
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from flask import Flask
from flask.testing import FlaskClient
from share_grants.agent_grants import load_agent_grants
from share_grants.agent_grants import load_agent_invites
from werkzeug.test import TestResponse

from share_gateway.agent_access import AgentAccessPaths
from share_gateway.agent_access import LastSeenStore
from share_gateway.agent_access import mint_invite
from share_gateway.agent_exec import MAX_RUN_BODY_BYTES
from share_gateway.agent_orientation import HELPER_PATH
from share_gateway.agent_orientation import helper_source_url
from share_gateway.agent_orientation import template_ref
from share_gateway.agent_routes import AgentAccessContext
from share_gateway.agent_routes import JoinRateLimiter
from share_gateway.agent_workspace import WorkspaceCoordinates
from share_gateway.caddyfile import RegisteredApp
from share_gateway.identity import IDENTITY_HEADER
from share_gateway.identity import RequesterIdentity
from share_gateway.session_cookie import PARTITIONED_SESSION_COOKIE_NAME
from share_gateway.session_cookie import SESSION_COOKIE_NAME
from share_gateway.session_cookie import mint_session_cookie_value
from share_gateway.testing import ProcessLifeline
from share_gateway.testing import build_agent_test_app
from share_gateway.testing import make_workspace_repo
from share_gateway.testing import new_ed25519_public_key_line
from share_gateway.testing import serve_wsgi

_DOMAIN = "alpha.7f3a.us1.example.com"
_AUTH_LABEL = "auth-x7k9q2w1"
_AUTH_HOST = f"{_AUTH_LABEL}.{_DOMAIN}"
_SIGNING_SECRET = "gateway-secret-4471-long-enough-for-hs256"
_BROKER_KEY = generate_private_key(public_exponent=65537, key_size=2048)
_APPS = [
    RegisteredApp("system_interface", "system_interface-sh111111", "127.0.0.1", 8000, is_grantable=False),
    RegisteredApp("terminal-pty", "terminal-pty-tp111111", "127.0.0.1", 7682, is_grantable=False),
    RegisteredApp("chat", "chat-ch111111", "127.0.0.1", 8010, is_grantable=True),
    RegisteredApp("files", "files-fi111111", "127.0.0.1", 8300, is_grantable=True),
]
_SHARE_GRANTS = """
[workspace]
users = ["user-visitor-whole"]
emails = []
email_domains = []

[services.chat]
users = ["user-visitor-chat"]
emails = []
email_domains = []
"""
_PRIMER = "# {{workspace_name}}\n<!-- exec-only:start -->\nEXEC\n<!-- exec-only:end -->\n{{skills_table}}\n{{apps_table}}\n{{connected_accounts}}\n{{changes}}\n"
_INVITE = "Join {{auth_origin}} with {{code}} ({{scope}})\n<!-- exec-only:start -->\nwsvisit run\n<!-- exec-only:end -->\n{{apps}}\n"
_NOW = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self) -> None:
        self.now = _NOW

    def __call__(self) -> datetime:
        return self.now


class _Harness:
    def __init__(
        self, app: Flask, client: FlaskClient, context: AgentAccessContext, clock: _Clock, repo: Path
    ) -> None:
        self.app = app
        self.client = client
        self.context = context
        self.clock = clock
        self.repo = repo


def make_harness(tmp_path: Path, chrome_origin: str = "") -> _Harness:
    repo = make_workspace_repo(tmp_path, _PRIMER, _INVITE)
    share_grants_path = tmp_path / "share_grants.toml"
    share_grants_path.write_text(_SHARE_GRANTS)
    clock = _Clock()
    context = AgentAccessContext(
        paths=AgentAccessPaths(
            grants_path=tmp_path / "agent_access" / "grants.toml",
            invites_path=tmp_path / "agent_access" / "invites.toml",
            events_dir=tmp_path / "events",
        ),
        coordinates=WorkspaceCoordinates(
            host_id="host-" + "a" * 32,
            workspace_id="agent-" + "b" * 32,
            display_name="Research Lab",
        ),
        repo_root=repo,
        environ={},
        last_seen=LastSeenStore(tmp_path / "state" / "last_seen.json"),
        skills_cache_path=tmp_path / "state" / "skills_index.json",
        now=clock,
    )
    app = build_agent_test_app(
        context, _APPS, share_grants_path, _DOMAIN, _AUTH_LABEL, _SIGNING_SECRET, chrome_origin=chrome_origin
    )
    return _Harness(app, app.test_client(), context, clock, repo)


def session_cookie(user_id: str, is_owner: bool = False) -> str:
    identity = RequesterIdentity(user_id=user_id, email=f"{user_id}@example.com", is_owner=is_owner)
    return mint_session_cookie_value(_SIGNING_SECRET, identity, _DOMAIN)


def join_as(
    harness: _Harness, scope: "str | list[str]", granter: str = "user:owner", name: str = "Josh Claude"
) -> dict[str, Any]:
    minted = mint_invite(harness.context.paths, scope, granter, "", None, harness.clock(), via="test")
    response = harness.client.post(
        "/_agent/join",
        json={"code": minted.code, "public_key": new_ed25519_public_key_line(), "name": name},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def bearer(document: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {document['token']}"}


def verify(harness: _Harness, host: str, headers: dict[str, str]) -> TestResponse:
    base = {"X-Forwarded-Host": host, "X-Forwarded-Method": "GET", "X-Forwarded-Uri": "/x"}
    return harness.client.get("/_auth/verify", headers={**base, **headers})


def run_events(response: TestResponse) -> list[dict[str, Any]]:
    """The events of a ``run`` answer, one per line of the stream."""
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines()]


def run_command(harness: _Harness, headers: dict[str, str], body: dict[str, Any]) -> list[dict[str, Any]]:
    response = harness.client.post("/_agent/run", json=body, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return run_events(response)


def encoded(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def test_join_answers_the_access_document_for_workspace_scope(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    document = join_as(harness, "workspace")
    assert document["scope"] == "workspace" and document["granter"] == "user:owner"
    assert document["name"] == "Josh Claude" and len(str(document["token"])) >= 40
    assert document["workspace"] == {
        "domain": _DOMAIN,
        "auth_origin": f"https://{_AUTH_HOST}",
        "host_id": "host-" + "a" * 32,
        "workspace_id": "agent-" + "b" * 32,
        "display_name": "Research Lab",
    }
    assert [app["name"] for app in document["apps"]] == ["chat", "files"]
    assert document["primer_url"] == f"https://{_AUTH_HOST}/_agent/primer"
    assert document["helper"] == {
        "version": template_ref(harness.context.repo_root),
        "source_url": helper_source_url(harness.context.repo_root),
        "sha256": hashlib.sha256(HELPER_PATH.read_bytes()).hexdigest(),
    }
    assert document["helper"]["source_url"].endswith(
        "/system/services/share_gateway/src/share_gateway/helper/wsvisit.cjs"
    )
    assert set(document) == {
        "agent_id",
        "name",
        "scope",
        "granter",
        "expires_at",
        "workspace",
        "apps",
        "primer_url",
        "helper",
        "exec",
        "token",
    }
    assert len(load_agent_grants(harness.context.paths.grants_path)) == 1


def test_join_for_an_app_scope_carries_only_the_named_apps(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    document = join_as(harness, ["files"])
    assert [app["name"] for app in document["apps"]] == ["files"]


def test_a_grant_of_the_whole_workspace_is_answered_an_empty_exec_member_at_join_and_at_me(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)

    document = join_as(harness, "workspace")
    me = harness.client.get("/_agent/me", headers=bearer(document)).get_json()

    # What a helper from before the gateway ran commands itself needs in order to print its notice to reinstall.
    assert document["exec"] == me["exec"] == {"origin": "", "audience": "", "host_public_key": ""}


def test_a_grant_for_named_apps_is_answered_no_exec_member_at_join_or_at_me(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)

    document = join_as(harness, ["files"])
    me = harness.client.get("/_agent/me", headers=bearer(document)).get_json()

    assert "exec" not in document
    assert "exec" not in me
    assert set(me) == set(document) - {"token"}


def test_join_refuses_malformed_bodies_bad_codes_and_bad_keys(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    headers: dict[str, str] = {}
    assert harness.client.post("/_agent/join", data="not json", headers=headers).status_code == 400
    oversized = json.dumps({"code": "x" * (20 * 1024), "public_key": "k", "name": "n"})
    assert (
        harness.client.post(
            "/_agent/join", data=oversized, content_type="application/json", headers=headers
        ).status_code
        == 400
    )
    assert (
        harness.client.post(
            "/_agent/join", json={"code": 1, "public_key": "x", "name": "n"}, headers=headers
        ).status_code
        == 400
    )
    bad_code = harness.client.post(
        "/_agent/join",
        json={"code": "AAAA-BBBB-CCCC", "public_key": new_ed25519_public_key_line(), "name": "n"},
        headers=headers,
    )
    assert bad_code.status_code == 403
    minted = mint_invite(
        harness.context.paths,
        "workspace",
        "user:owner",
        "",
        None,
        harness.clock(),
        via="test",
    )
    bad_key = harness.client.post(
        "/_agent/join", json={"code": minted.code, "public_key": "ssh-rsa AAAA", "name": "n"}, headers=headers
    )
    assert bad_key.status_code == 400
    # The invite survived the bad key (a failed attempt was counted against it), so the right key still joins.
    good = harness.client.post(
        "/_agent/join",
        json={"code": minted.code, "public_key": new_ed25519_public_key_line(), "name": "n"},
        headers=headers,
    )
    assert good.status_code == 200


def test_join_is_rate_limited_per_client_address(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    statuses = []
    for _ in range(11):
        response = harness.client.post(
            "/_agent/join",
            json={"code": "AAAA-BBBB-CCCC", "public_key": new_ed25519_public_key_line(), "name": "n"},
            headers={"X-Forwarded-For": "198.51.100.7"},
        )
        statuses.append(response.status_code)
    assert statuses[:10] == [403] * 10 and statuses[10] == 429
    other = harness.client.post(
        "/_agent/join",
        json={"code": "AAAA-BBBB-CCCC", "public_key": new_ed25519_public_key_line(), "name": "n"},
        headers={"X-Forwarded-For": "198.51.100.8"},
    )
    assert other.status_code == 403


def test_join_rate_limiter_forgets_addresses_whose_window_has_passed() -> None:
    limiter = JoinRateLimiter(attempts_per_minute=2)
    for index in range(1500):
        assert limiter.allow(f"203.0.113.{index}", 0.0)
    assert limiter.tracked_address_count == 1500
    assert limiter.allow("198.51.100.1", 61.0)
    assert limiter.tracked_address_count == 1
    # An address still inside its window keeps its attempts across a sweep.
    assert limiter.allow("198.51.100.1", 62.0)
    for index in range(1500):
        limiter.allow(f"203.0.113.{index}", 62.0)
    assert not limiter.allow("198.51.100.1", 63.0)


def test_me_requires_a_live_token_and_repeats_the_coordinates(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    document = join_as(harness, "workspace")
    assert harness.client.get("/_agent/me", headers={}).status_code == 401
    assert harness.client.get("/_agent/me", headers={"Authorization": "Bearer nope"}).status_code == 403
    me = harness.client.get("/_agent/me", headers=bearer(document)).get_json()
    assert me == {key: value for key, value in document.items() if key != "token"}
    assert harness.context.last_seen.read_all()[str(document["agent_id"])] == _NOW.isoformat()


def test_primer_is_rendered_for_the_scope(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    full = harness.client.get("/_agent/primer", headers=bearer(join_as(harness, "workspace")))
    assert full.status_code == 200 and full.mimetype == "text/markdown"
    text = full.get_data(as_text=True)
    assert text.startswith("# Research Lab") and "EXEC" in text and "build-app" in text
    assert "exec-only" not in text
    assert "no latchkey gateway" in text and "Initial workspace commit" in text
    reduced = harness.client.get("/_agent/primer", headers=bearer(join_as(harness, ["chat"]))).get_data(as_text=True)
    assert (
        "EXEC" not in reduced and "Not applicable" in reduced and "| chat |" in reduced and "| files |" not in reduced
    )


def test_skills_and_changes_routes(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    headers = bearer(join_as(harness, "workspace"))
    skills = harness.client.get("/_agent/skills", headers=headers).get_json()
    assert [skill["name"] for skill in skills["skills"]] == ["build-app", "notify-user"]
    changes = harness.client.get("/_agent/changes", headers=headers).get_json()
    assert [entry["subject"] for entry in changes["commits"]] == ["Initial workspace commit"]
    assert harness.client.get("/_agent/changes?since=yesterday", headers=headers).status_code == 400
    far_future = quote((datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    assert harness.client.get(f"/_agent/changes?since={far_future}", headers=headers).get_json()["commits"] == []
    assert harness.client.get("/_agent/skills", headers={}).status_code == 401


def test_helper_is_served_without_caching(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    response = harness.client.get("/_agent/helper", headers={})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store" and response.headers["X-Helper-Version"]
    assert response.headers["X-Helper-SHA256"] == hashlib.sha256(response.get_data()).hexdigest()
    assert response.get_data(as_text=True).startswith("#!/usr/bin/env node")


def test_invites_need_workspace_level_access_human_or_agent(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    headers: dict[str, str] = {}
    assert harness.client.post("/_agent/invites", json={}, headers=headers).status_code == 401

    harness.client.set_cookie(SESSION_COOKIE_NAME, session_cookie("user-owner", is_owner=True))
    owner = harness.client.post(
        "/_agent/invites", json={"scope": "workspace", "name_hint": "Josh Claude"}, headers=headers
    )
    assert owner.status_code == 200
    body = owner.get_json()
    assert body["granter"] == "user:user-owner" and body["scope"] == "workspace"
    assert body["code"] in body["instructions"] and f"https://{_AUTH_HOST}" in body["instructions"]
    assert "wsvisit run" in body["instructions"]

    harness.client.set_cookie(SESSION_COOKIE_NAME, session_cookie("user-visitor-whole"))
    whole = harness.client.post("/_agent/invites", json={"scope": ["chat"]}, headers=headers)
    assert whole.status_code == 200 and whole.get_json()["granter"] == "user:user-visitor-whole"
    assert "wsvisit run" not in whole.get_json()["instructions"]
    assert harness.client.post("/_agent/invites", json={"scope": ["terminal-pty"]}, headers=headers).status_code == 400
    assert harness.client.post("/_agent/invites", json={"expires_in_days": -1}, headers=headers).status_code == 400

    harness.client.set_cookie(SESSION_COOKIE_NAME, session_cookie("user-visitor-chat"))
    assert harness.client.post("/_agent/invites", json={}, headers=headers).status_code == 403
    harness.client.delete_cookie(SESSION_COOKIE_NAME)

    workspace_agent = join_as(harness, "workspace")
    by_agent = harness.client.post("/_agent/invites", json={"scope": "workspace"}, headers=bearer(workspace_agent))
    assert by_agent.status_code == 200 and by_agent.get_json()["granter"] == f"agent:{workspace_agent['agent_id']}"
    app_agent = join_as(harness, ["chat"])
    assert harness.client.post("/_agent/invites", json={}, headers=bearer(app_agent)).status_code == 403


def test_grant_list_and_revocation_follow_ownership(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    headers: dict[str, str] = {}
    by_owner = join_as(harness, "workspace", granter="user:user-owner", name="owner-agent")
    by_visitor = join_as(harness, "workspace", granter="user:user-visitor-whole", name="visitor-agent")

    harness.client.set_cookie(SESSION_COOKIE_NAME, session_cookie("user-visitor-whole"))
    visitor_view = harness.client.get("/_agent/grants", headers=headers).get_json()["agents"]
    assert [agent["name"] for agent in visitor_view] == ["visitor-agent"]
    assert "token_sha256" not in visitor_view[0] and "public_key" not in visitor_view[0]
    assert harness.client.delete(f"/_agent/grants/{by_owner['agent_id']}", headers=headers).status_code == 404
    assert harness.client.delete("/_agent/grants/not-an-id", headers=headers).status_code == 400

    harness.client.set_cookie(SESSION_COOKIE_NAME, session_cookie("user-owner", is_owner=True))
    owner_view = harness.client.get("/_agent/grants", headers=headers).get_json()["agents"]
    assert sorted(agent["name"] for agent in owner_view) == ["owner-agent", "visitor-agent"]
    assert harness.client.delete(f"/_agent/grants/{by_visitor['agent_id']}", headers=headers).status_code == 200
    harness.client.delete_cookie(SESSION_COOKIE_NAME)

    # The revoked agent's token is dead; the other's still works and sees only what it granted.
    assert harness.client.get("/_agent/me", headers=bearer(by_visitor)).status_code == 403
    agent_view = harness.client.get("/_agent/grants", headers=bearer(by_owner)).get_json()["agents"]
    assert agent_view == []


_CHROME_ORIGIN = "https://studio.example.com"
_FOREIGN_ORIGIN = {"Origin": "https://evil.example.com"}


def test_a_session_request_from_another_sites_page_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, chrome_origin=_CHROME_ORIGIN)
    agent = join_as(harness, "workspace", granter="user:user-owner")
    for cookie_name in (SESSION_COOKIE_NAME, PARTITIONED_SESSION_COOKIE_NAME):
        harness.client.set_cookie(cookie_name, session_cookie("user-owner", is_owner=True))
        # A text/plain body is one a page may send to another origin with no preflight.
        invite = harness.client.post(
            "/_agent/invites", data='{"scope": "workspace"}', content_type="text/plain", headers=_FOREIGN_ORIGIN
        )
        assert invite.status_code == 403, cookie_name
        assert invite.get_json() == {"error": "this request's Origin is not allowed"}
        revoke = harness.client.delete(f"/_agent/grants/{agent['agent_id']}", headers=_FOREIGN_ORIGIN)
        assert revoke.status_code == 403, cookie_name
        harness.client.delete_cookie(cookie_name)
    assert load_agent_invites(harness.context.paths.invites_path) == []
    assert [grant.agent_id for grant in load_agent_grants(harness.context.paths.grants_path)] == [agent["agent_id"]]


def test_a_session_request_from_the_workspaces_own_pages_or_from_no_page_is_served(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, chrome_origin=_CHROME_ORIGIN)
    harness.client.set_cookie(SESSION_COOKIE_NAME, session_cookie("user-owner", is_owner=True))
    for origin in (f"https://{_AUTH_HOST}", f"https://chat-ch111111.{_DOMAIN}", _CHROME_ORIGIN):
        response = harness.client.post("/_agent/invites", json={}, headers={"Origin": origin})
        assert response.status_code == 200, origin
    assert harness.client.post("/_agent/invites", json={}, headers={}).status_code == 200
    # A read is exempt, as it is for an app: it changes nothing, and its answer is not readable across origins.
    assert harness.client.get("/_agent/grants", headers=_FOREIGN_ORIGIN).status_code == 200
    assert len(load_agent_invites(harness.context.paths.invites_path)) == 4


def test_a_request_without_a_session_is_told_to_authenticate_whatever_its_origin(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    assert harness.client.post("/_agent/invites", json={}, headers=_FOREIGN_ORIGIN).status_code == 401


def test_a_bearer_request_is_not_held_to_the_origin_policy(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    headers = {**bearer(join_as(harness, "workspace")), **_FOREIGN_ORIGIN}
    assert harness.client.post("/_agent/invites", json={}, headers=headers).status_code == 200
    assert harness.client.post("/_agent/run", json={"command": ["true"]}, headers=headers).status_code == 200


def test_verify_admits_a_workspace_scope_agent_everywhere_with_the_agent_identity(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    document = join_as(harness, "workspace")
    harness.client.set_cookie(SESSION_COOKIE_NAME, "stale")
    harness.client.set_cookie("theme", "dark")
    headers = {"Authorization": f"Bearer {document['token']}"}
    for host in (
        f"terminal-pty-tp111111.{_DOMAIN}",
        f"system_interface-sh111111.{_DOMAIN}",
        f"chat-ch111111.{_DOMAIN}",
    ):
        response = verify(harness, host, headers)
        assert response.status_code == 200, host
        identity = json.loads(response.headers[IDENTITY_HEADER])
        assert identity == {
            "owner": False,
            "agent_id": document["agent_id"],
            "agent_name": "Josh Claude",
            "granter": "user:owner",
        }
        assert response.headers["X-Share-Filtered-Cookie"] == "theme=dark"


def test_verify_holds_an_app_scope_agent_to_its_apps_and_refuses_bad_tokens(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    document = join_as(harness, ["chat"])
    headers = {"Authorization": f"Bearer {document['token']}"}
    assert verify(harness, f"chat-ch111111.{_DOMAIN}", headers).status_code == 200
    assert verify(harness, f"files-fi111111.{_DOMAIN}", headers).status_code == 403
    assert verify(harness, f"terminal-pty-tp111111.{_DOMAIN}", headers).status_code == 403
    assert verify(harness, f"system_interface-sh111111.{_DOMAIN}", headers).status_code == 403
    assert verify(harness, f"chat-ch111111.{_DOMAIN}", {"Authorization": "Bearer nope"}).status_code == 403
    assert verify(harness, f"chat-ch111111.{_DOMAIN}", {"Authorization": "Basic abc"}).status_code == 401


def test_verify_skips_the_origin_policy_for_bearer_requests(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    document = join_as(harness, "workspace")
    headers = {
        "Authorization": f"Bearer {document['token']}",
        "X-Forwarded-Method": "POST",
        "X-Forwarded-Upgrade": "websocket",
        "Origin": "https://evil.example.com",
    }
    assert verify(harness, f"chat-ch111111.{_DOMAIN}", headers).status_code == 200


def test_verify_refuses_an_expired_grant_and_an_unconfigured_gateway(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    minted = mint_invite(
        harness.context.paths,
        "workspace",
        "user:owner",
        "",
        timedelta(hours=1),
        harness.clock(),
        via="test",
    )
    response = harness.client.post(
        "/_agent/join",
        json={"code": minted.code, "public_key": new_ed25519_public_key_line(), "name": "n"},
        headers={},
    )
    document = response.get_json()
    headers = {"Authorization": f"Bearer {document['token']}"}
    assert verify(harness, f"chat-ch111111.{_DOMAIN}", headers).status_code == 200
    harness.clock.now = _NOW + timedelta(hours=2)
    assert verify(harness, f"chat-ch111111.{_DOMAIN}", headers).status_code == 403
    assert harness.client.get("/_agent/me", headers=bearer(document)).status_code == 403


# One request per route, each of which would leave a file in the repository if it were let through.
_WORKSPACE_AUTHORITY_REQUESTS: list[tuple[str, dict[str, Any]]] = [
    ("/_agent/run", {"command": ["touch", "left-by-run"]}),
    ("/_agent/files/read", {"path": "left-by-read"}),
    ("/_agent/files/write", {"path": "left-by-write", "content_b64": ""}),
]


def test_run_streams_the_commands_output_as_json_lines_and_ends_with_its_exit_code(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    response = harness.client.post(
        "/_agent/run",
        json={"command": ["sh", "-c", "echo one; echo two; echo trouble >&2; exit 4"]},
        headers=bearer(join_as(harness, "workspace")),
    )
    assert response.status_code == 200 and response.mimetype == "application/x-ndjson"
    assert response.get_data(as_text=True).endswith("\n")
    events = run_events(response)
    assert [event for event in events if event["type"] == "stdout"] == [
        {"type": "stdout", "data": "one\n"},
        {"type": "stdout", "data": "two\n"},
    ]
    assert [event for event in events if event["type"] == "stderr"] == [{"type": "stderr", "data": "trouble\n"}]
    assert events[-1] == {"type": "exit", "code": 4}
    # Nothing signs the stream: the token on the request is the whole credential.
    assert {event["type"] for event in events} == {"stdout", "stderr", "exit"}
    assert "Signature" not in response.headers and "Content-Digest" not in response.headers


def test_run_starts_in_the_repository_root_unless_the_request_names_a_directory(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    headers = bearer(join_as(harness, "workspace"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert run_command(harness, headers, {"command": ["pwd"]})[0]["data"] == f"{harness.repo.resolve()}\n"
    in_repo = run_command(harness, headers, {"command": ["pwd"], "cwd": ".agents/skills"})
    assert in_repo[0]["data"] == f"{(harness.repo / '.agents' / 'skills').resolve()}\n"
    absolute = run_command(harness, headers, {"command": ["pwd"], "cwd": str(elsewhere)})
    assert absolute == [{"type": "stdout", "data": f"{elsewhere.resolve()}\n"}, {"type": "exit", "code": 0}]


def test_run_delivers_each_line_while_the_command_is_still_running(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    go_on = tmp_path / "go-on"
    script = f"echo first; while [ ! -e {go_on} ]; do sleep 0.01; done; echo second"
    response = harness.client.post(
        "/_agent/run",
        json={"command": ["sh", "-c", script]},
        headers=bearer(join_as(harness, "workspace")),
        buffered=False,
    )
    lines = response.iter_encoded()
    # The command cannot print its second line until this test, having read the first, lets it.
    assert json.loads(next(lines)) == {"type": "stdout", "data": "first\n"}
    go_on.touch()
    assert [json.loads(line) for line in lines] == [
        {"type": "stdout", "data": "second\n"},
        {"type": "exit", "code": 0},
    ]


def test_run_out_of_time_ends_the_stream_with_an_exit_that_says_so(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    events = run_command(
        harness, bearer(join_as(harness, "workspace")), {"command": ["sleep", "600"], "timeout_seconds": 0.2}
    )
    # A request may not ask for less than a second, so the stream ends a second in, not ten minutes.
    assert events == [{"type": "exit", "code": None, "timed_out": True}]


def test_a_run_nobody_reads_to_its_end_stops_the_command_and_everything_it_started(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    lifeline = ProcessLifeline(tmp_path)
    try:
        response = harness.client.post(
            "/_agent/run",
            json={"command": lifeline.holding_command()},
            headers=bearer(join_as(harness, "workspace")),
            buffered=False,
        )
        assert json.loads(next(response.iter_encoded())) == {"type": "stdout", "data": "started\n"}
        assert not lifeline.is_released_within(0.0)
        response.close()
        assert lifeline.is_released_within(5.0)
    finally:
        lifeline.close()
        harness.context.running_commands.kill_all()


def test_a_caller_that_hangs_up_on_a_run_that_prints_nothing_stops_the_command(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    lifeline = ProcessLifeline(tmp_path)
    headers = bearer(join_as(harness, "workspace"))
    body = {"command": lifeline.holding_command()}
    try:
        with serve_wsgi(harness.app, is_threaded=True) as origin:
            with httpx.stream("POST", f"{origin}/_agent/run", json=body, headers=headers, timeout=10.0) as response:
                assert response.status_code == 200 and response.headers["Transfer-Encoding"] == "chunked"
                # Held for the life of the block: an iterator that is dropped closes the connection at once,
                # and the command would be stopped before the check below.
                lines = response.iter_lines()
                assert json.loads(next(lines)) == {"type": "stdout", "data": "started\n"}
                assert not lifeline.is_released_within(0.0)
            # Leaving the block closed the connection on an answer that had ten minutes left to run.
            assert lifeline.is_released_within(5.0)
    finally:
        lifeline.close()
        harness.context.running_commands.kill_all()


def test_run_refuses_a_body_it_cannot_act_on(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    headers = bearer(join_as(harness, "workspace"))
    not_json = harness.client.post("/_agent/run", data="not json", headers=headers)
    assert not_json.status_code == 400 and not_json.get_json() == {"error": "the body must be a JSON object"}
    assert harness.client.post("/_agent/run", json=["ls"], headers=headers).status_code == 400
    error_by_body: list[tuple[dict[str, Any], str]] = [
        ({}, "command must be a non-empty list of strings"),
        ({"command": []}, "command must be a non-empty list of strings"),
        ({"command": "ls -la"}, "command must be a non-empty list of strings"),
        ({"command": ["ls", 1]}, "command must be a non-empty list of strings"),
        ({"command": ["ls"], "cwd": 5}, "cwd must be a string"),
        ({"command": ["ls"], "timeout_seconds": "30"}, "timeout_seconds must be a number of seconds"),
    ]
    huge_timeout = harness.client.post(
        "/_agent/run",
        data='{"command": ["true"], "timeout_seconds": 1' + "0" * 400 + "}",
        content_type="application/json",
        headers=headers,
    )
    assert run_events(huge_timeout) == [{"type": "exit", "code": 0}]
    for body, error in error_by_body:
        response = harness.client.post("/_agent/run", json=body, headers=headers)
        assert response.status_code == 400 and response.get_json() == {"error": error}, body
    oversized = json.dumps({"command": ["echo", "x" * MAX_RUN_BODY_BYTES]})
    too_large = harness.client.post("/_agent/run", data=oversized, content_type="application/json", headers=headers)
    assert too_large.status_code == 413 and too_large.get_json() == {"error": "the body is too large"}
    assert harness.client.get("/_agent/run", headers=headers).status_code == 405


def test_a_command_that_cannot_start_is_a_stream_of_one_exit_event(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    events = run_command(harness, bearer(join_as(harness, "workspace")), {"command": ["no-such-command-77c1"]})
    assert len(events) == 1 and events[0]["type"] == "exit" and events[0]["code"] is None
    assert "no-such-command-77c1" in events[0]["error"]


def test_commands_and_files_are_refused_without_a_valid_token(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    for route, body in _WORKSPACE_AUTHORITY_REQUESTS:
        missing = harness.client.post(route, json=body, headers={})
        assert missing.status_code == 401 and missing.get_json() == {"error": "authentication required"}, route
        invalid = harness.client.post(route, json=body, headers={"Authorization": "Bearer nope"})
        assert invalid.status_code == 403 and invalid.get_json() == {"error": "this access token is not valid"}, route
        assert harness.client.post(route, json=body, headers={"Authorization": "Basic abc"}).status_code == 401, route
    assert sorted(entry.name for entry in harness.repo.iterdir() if entry.name.startswith("left-by")) == []


def test_commands_and_files_are_refused_to_the_owners_session(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    harness.client.set_cookie(SESSION_COOKIE_NAME, session_cookie("user-owner", is_owner=True))
    # The owner's session does reach the routes that take one, so the refusals below are these routes' own.
    assert harness.client.get("/_agent/grants", headers={}).status_code == 200
    for route, body in _WORKSPACE_AUTHORITY_REQUESTS:
        assert harness.client.post(route, json=body, headers={}).status_code == 401, route
    assert sorted(entry.name for entry in harness.repo.iterdir() if entry.name.startswith("left-by")) == []


def test_commands_and_files_are_refused_to_a_grant_scoped_to_an_app(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    headers = bearer(join_as(harness, ["chat", "files"]))
    assert harness.client.get("/_agent/me", headers=headers).status_code == 200
    for route, body in _WORKSPACE_AUTHORITY_REQUESTS:
        response = harness.client.post(route, json=body, headers=headers)
        assert response.status_code == 403, route
        assert "needs access to the whole workspace" in response.get_json()["error"], route
    assert sorted(entry.name for entry in harness.repo.iterdir() if entry.name.startswith("left-by")) == []


def test_revoking_a_grant_ends_commands_and_files_at_the_next_request(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    document = join_as(harness, "workspace", granter="user:user-owner")
    headers = bearer(document)
    assert run_command(harness, headers, {"command": ["true"]}) == [{"type": "exit", "code": 0}]
    harness.client.set_cookie(SESSION_COOKIE_NAME, session_cookie("user-owner", is_owner=True))
    assert harness.client.delete(f"/_agent/grants/{document['agent_id']}", headers={}).status_code == 200
    harness.client.delete_cookie(SESSION_COOKIE_NAME)
    for route, body in _WORKSPACE_AUTHORITY_REQUESTS:
        assert harness.client.post(route, json=body, headers=headers).status_code == 403, route
    assert sorted(entry.name for entry in harness.repo.iterdir() if entry.name.startswith("left-by")) == []


def test_an_expired_grant_ends_commands_and_files(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    minted = mint_invite(
        harness.context.paths, "workspace", "user:owner", "", timedelta(hours=1), harness.clock(), via="test"
    )
    joined = harness.client.post(
        "/_agent/join",
        json={"code": minted.code, "public_key": new_ed25519_public_key_line(), "name": "n"},
        headers={},
    )
    headers = bearer(joined.get_json())
    written = harness.client.post(
        "/_agent/files/write", json={"path": "before-expiry", "content_b64": ""}, headers=headers
    )
    assert written.status_code == 200 and (harness.repo / "before-expiry").exists()
    harness.clock.now = _NOW + timedelta(hours=2)
    for route, body in _WORKSPACE_AUTHORITY_REQUESTS:
        assert harness.client.post(route, json=body, headers=headers).status_code == 403, route
    assert sorted(entry.name for entry in harness.repo.iterdir() if entry.name.startswith("left-by")) == []


def test_files_read_answers_the_contents_of_a_path_in_the_repository_or_anywhere_else(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    headers = bearer(join_as(harness, "workspace"))
    (harness.repo / "data").mkdir()
    (harness.repo / "data" / "note.bin").write_bytes(b"\x00a note\xff\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside the repository\n")
    in_repo = harness.client.post("/_agent/files/read", json={"path": "data/note.bin"}, headers=headers)
    assert in_repo.status_code == 200 and in_repo.mimetype == "application/json"
    assert in_repo.get_json() == {"exists": True, "content_b64": encoded(b"\x00a note\xff\n")}
    absolute = harness.client.post("/_agent/files/read", json={"path": str(outside)}, headers=headers)
    assert absolute.get_json() == {"exists": True, "content_b64": encoded(b"outside the repository\n")}
    assert "Signature" not in in_repo.headers and "Content-Digest" not in in_repo.headers


def test_files_read_says_a_missing_file_does_not_exist_and_refuses_what_it_cannot_read(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    headers = bearer(join_as(harness, "workspace"))
    missing = harness.client.post("/_agent/files/read", json={"path": "data/absent.txt"}, headers=headers)
    assert missing.status_code == 404 and missing.get_json() == {"exists": False}
    directory = harness.client.post("/_agent/files/read", json={"path": ".agents"}, headers=headers)
    assert directory.status_code == 400
    assert directory.get_json() == {"error": "could not read .agents: it is not a regular file"}
    for body in ({}, {"path": ""}, {"path": 5}):
        response = harness.client.post("/_agent/files/read", json=body, headers=headers)
        assert response.status_code == 400 and response.get_json() == {"error": "path must be a non-empty string"}
    null_byte = harness.client.post("/_agent/files/read", json={"path": "data/a\x00b"}, headers=headers)
    assert null_byte.status_code == 400 and null_byte.get_json() == {"error": "path is not one a file can have"}
    oversized = json.dumps({"path": "x" * (20 * 1024)})
    too_large = harness.client.post(
        "/_agent/files/read", data=oversized, content_type="application/json", headers=headers
    )
    assert too_large.status_code == 413


def test_files_write_then_read_round_trips_the_bytes_and_honours_the_mode(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    headers = bearer(join_as(harness, "workspace"))
    content = bytes(range(256)) * 4
    written = harness.client.post(
        "/_agent/files/write",
        json={"path": "data/made/on-the-way/blob.bin", "content_b64": encoded(content), "mode": "640"},
        headers=headers,
    )
    assert written.status_code == 200 and written.get_json() == {"written": True}
    target = harness.repo / "data" / "made" / "on-the-way" / "blob.bin"
    assert target.read_bytes() == content and stat.S_IMODE(target.stat().st_mode) == 0o640
    read_back = harness.client.post(
        "/_agent/files/read", json={"path": "data/made/on-the-way/blob.bin"}, headers=headers
    )
    assert read_back.get_json() == {"exists": True, "content_b64": encoded(content)}

    replaced = harness.client.post(
        "/_agent/files/write",
        json={"path": str(target), "content_b64": encoded(b"shorter\n")},
        headers=headers,
    )
    assert replaced.status_code == 200
    # No mode asked for is owner-only, whatever the file was before.
    assert target.read_bytes() == b"shorter\n" and stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [entry.name for entry in target.parent.iterdir()] == ["blob.bin"]


def test_files_write_refuses_a_body_it_cannot_act_on_and_writes_nothing(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    headers = bearer(join_as(harness, "workspace"))
    error_by_body: list[tuple[dict[str, Any], str]] = [
        ({"content_b64": ""}, "path must be a non-empty string"),
        ({"path": "", "content_b64": ""}, "path must be a non-empty string"),
        ({"path": "refused.txt"}, "content_b64 must be a string"),
        ({"path": "refused.txt", "content_b64": "not base64!"}, "content_b64 is not valid base64"),
        (
            {"path": "refused.txt", "content_b64": "", "mode": "rw"},
            "mode must be the octal digits of permission bits, like 644",
        ),
        (
            {"path": "refused.txt", "content_b64": "", "mode": "4755"},
            "mode must be the octal digits of permission bits, like 644",
        ),
        ({"path": ".agents", "content_b64": ""}, "could not write .agents: Is a directory"),
    ]
    for body, error in error_by_body:
        response = harness.client.post("/_agent/files/write", json=body, headers=headers)
        assert response.status_code == 400 and response.get_json() == {"error": error}, body
    assert not (harness.repo / "refused.txt").exists()
    assert sorted(entry.name for entry in harness.repo.iterdir()) == [".agents", ".git"]
