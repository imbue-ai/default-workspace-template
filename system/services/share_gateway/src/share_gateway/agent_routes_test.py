import hashlib
import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from flask import Flask
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

from share_gateway.agent_access import AgentAccessPaths
from share_gateway.agent_access import LastSeenStore
from share_gateway.agent_access import mint_invite
from share_gateway.agent_grants import load_agent_grants
from share_gateway.agent_orientation import HELPER_PATH
from share_gateway.agent_orientation import helper_source_url
from share_gateway.agent_orientation import template_ref
from share_gateway.agent_routes import AgentAccessContext
from share_gateway.agent_routes import JoinRateLimiter
from share_gateway.agent_workspace import WorkspaceCoordinates
from share_gateway.caddyfile import RegisteredApp
from share_gateway.identity import IDENTITY_HEADER
from share_gateway.identity import RequesterIdentity
from share_gateway.session_cookie import SESSION_COOKIE_NAME
from share_gateway.session_cookie import mint_session_cookie_value
from share_gateway.testing import build_agent_test_app
from share_gateway.testing import make_workspace_repo
from share_gateway.testing import new_ed25519_public_key_line

_DOMAIN = "alpha.7f3a.us1.example.com"
_AUTH_LABEL = "auth-x7k9q2w1"
_AUTH_HOST = f"{_AUTH_LABEL}.{_DOMAIN}"
_SIGNING_SECRET = "gateway-secret-4471-long-enough-for-hs256"
_BROKER_KEY = generate_private_key(public_exponent=65537, key_size=2048)
_APPS = [
    RegisteredApp("system_interface", "system_interface-sh111111", "127.0.0.1", 8000, is_grantable=False),
    RegisteredApp("owner-exec", "owner-exec-ex111111", "127.0.0.1", 8793, is_grantable=False),
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


def make_harness(tmp_path: Path) -> _Harness:
    repo = make_workspace_repo(tmp_path, _PRIMER, _INVITE)
    share_grants_path = tmp_path / "share_grants.toml"
    share_grants_path.write_text(_SHARE_GRANTS)
    host_key = tmp_path / "host_key.pub"
    host_key.write_text("ssh-ed25519 AAAAhostkey root@box\n")
    clock = _Clock()
    context = AgentAccessContext(
        paths=AgentAccessPaths(
            grants_path=tmp_path / "agent_access" / "grants.toml",
            invites_path=tmp_path / "agent_access" / "invites.toml",
            authorized_keys_path=tmp_path / "ssh" / "authorized_keys",
            events_dir=tmp_path / "events",
        ),
        coordinates=WorkspaceCoordinates(
            host_id="host-" + "a" * 32,
            workspace_id="agent-" + "b" * 32,
            display_name="Research Lab",
            host_public_key="ssh-ed25519 AAAAhostkey root@box",
        ),
        repo_root=repo,
        environ={},
        last_seen=LastSeenStore(tmp_path / "state" / "last_seen.json"),
        skills_cache_path=tmp_path / "state" / "skills_index.json",
        now=clock,
    )
    app = build_agent_test_app(context, _APPS, share_grants_path, _DOMAIN, _AUTH_LABEL, _SIGNING_SECRET)
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


# --- join ---


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
    assert document["exec"] == {
        "origin": f"https://owner-exec-ex111111.{_DOMAIN}",
        "audience": "container:host-" + "a" * 32,
        "host_public_key": "ssh-ed25519 AAAAhostkey root@box",
    }
    assert [app["name"] for app in document["apps"]] == ["chat", "files"]
    assert document["primer_url"] == f"https://{_AUTH_HOST}/_agent/primer"
    assert document["helper"] == {
        "version": template_ref(harness.context.repo_root),
        "source_url": helper_source_url(harness.context.repo_root),
        "sha256": hashlib.sha256(HELPER_PATH.read_bytes()).hexdigest(),
    }
    assert document["helper"]["source_url"].endswith("/system/services/share_gateway/src/share_gateway/helper/wsvisit.cjs")
    assert len(load_agent_grants(harness.context.paths.grants_path)) == 1


def test_join_for_an_app_scope_carries_no_exec_and_only_the_named_apps(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    document = join_as(harness, ["files"])
    assert "exec" not in document
    assert [app["name"] for app in document["apps"]] == ["files"]


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


# --- me, primer, skills, changes ---


def test_me_requires_a_live_token_and_repeats_the_coordinates(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    document = join_as(harness, "workspace")
    assert harness.client.get("/_agent/me", headers={}).status_code == 401
    assert harness.client.get("/_agent/me", headers={"Authorization": "Bearer nope"}).status_code == 403
    me = harness.client.get("/_agent/me", headers=bearer(document)).get_json()
    assert "token" not in me
    assert me["exec"] == document["exec"] and me["agent_id"] == document["agent_id"]
    assert me["helper"] == document["helper"]
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


# --- invites ---


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
    assert harness.client.post("/_agent/invites", json={"scope": ["owner-exec"]}, headers=headers).status_code == 400
    assert harness.client.post("/_agent/invites", json={"expires_in_days": -1}, headers=headers).status_code == 400

    harness.client.set_cookie(SESSION_COOKIE_NAME, session_cookie("user-visitor-chat"))
    assert harness.client.post("/_agent/invites", json={}, headers=headers).status_code == 403
    harness.client.delete_cookie(SESSION_COOKIE_NAME)

    workspace_agent = join_as(harness, "workspace")
    by_agent = harness.client.post("/_agent/invites", json={"scope": "workspace"}, headers=bearer(workspace_agent))
    assert by_agent.status_code == 200 and by_agent.get_json()["granter"] == f"agent:{workspace_agent['agent_id']}"
    app_agent = join_as(harness, ["chat"])
    assert harness.client.post("/_agent/invites", json={}, headers=bearer(app_agent)).status_code == 403


# --- grants ---


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


# --- admission at /_auth/verify ---


def test_verify_admits_a_workspace_scope_agent_everywhere_with_the_agent_identity(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    document = join_as(harness, "workspace")
    harness.client.set_cookie(SESSION_COOKIE_NAME, "stale")
    harness.client.set_cookie("theme", "dark")
    headers = {"Authorization": f"Bearer {document['token']}"}
    for host in (f"owner-exec-ex111111.{_DOMAIN}", f"system_interface-sh111111.{_DOMAIN}", f"chat-ch111111.{_DOMAIN}"):
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
    assert verify(harness, f"owner-exec-ex111111.{_DOMAIN}", headers).status_code == 403
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
