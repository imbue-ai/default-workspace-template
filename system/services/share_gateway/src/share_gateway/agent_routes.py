"""The gateway's ``/_agent/*`` surface on the auth origin: join, orientation, invites, and the grant list.

These routes are served by the gateway's Flask app directly (caddy routes the
auth origin's ``/_agent/*`` to it beside ``/_auth/*``), not through
``forward_auth``, so each does its own authentication: a session cookie
yields a human identity (the owner or a visitor), a bearer admission token a
visiting agent's. The join route is the one code-gated, otherwise
unauthenticated entry, rate limited per client address.
"""

import hashlib
import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from flask import Flask
from flask import Response
from flask import request

from share_gateway.agent_access import AgentAccessPaths
from share_gateway.agent_access import InvalidGrantLifetimeError
from share_gateway.agent_access import JoinRefusedError
from share_gateway.agent_access import LastSeenStore
from share_gateway.agent_access import RevokeRefusedError
from share_gateway.agent_access import count_failed_join
from share_gateway.agent_access import describe_grant
from share_gateway.agent_access import grant_lifetime_from_days
from share_gateway.agent_access import join
from share_gateway.agent_access import list_grants
from share_gateway.agent_access import mint_invite
from share_gateway.agent_access import revoke
from share_gateway.agent_access import visible_grants
from share_gateway.agent_grants import AgentGrant
from share_gateway.agent_grants import AgentGrantsError
from share_gateway.agent_grants import AgentScopeError
from share_gateway.agent_grants import find_grant_by_token
from share_gateway.agent_grants import is_agent_id
from share_gateway.agent_grants import load_active_agent_grants
from share_gateway.agent_grants import validate_scope
from share_gateway.agent_orientation import HELPER_PATH
from share_gateway.agent_orientation import LatchkeySummaryCache
from share_gateway.agent_orientation import build_changes_feed
from share_gateway.agent_orientation import cached_skills_index
from share_gateway.agent_orientation import default_changes_since
from share_gateway.agent_orientation import file_sha256
from share_gateway.agent_orientation import helper_source_url
from share_gateway.agent_orientation import parse_since
from share_gateway.agent_orientation import render_instruction_block
from share_gateway.agent_orientation import render_primer
from share_gateway.agent_orientation import template_ref
from share_gateway.agent_workspace import WorkspaceCoordinates
from share_gateway.agent_workspace import app_origins_for
from share_gateway.agent_workspace import exec_origin_for
from share_gateway.caddyfile import RegisteredApp
from share_gateway.caddyfile import build_grantable_service_names
from share_gateway.grants import GrantsError
from share_gateway.grants import load_grants
from share_gateway.identity import RequesterIdentity
from share_gateway.identity import bearer_token_from_headers
from share_gateway.json_values import is_json_object
from share_gateway.log import log
from share_gateway.server import forwarded_client_ip
from share_gateway.session_cookie import verify_session_from_cookies

_JOIN_ATTEMPTS_PER_MINUTE = 10
_JOIN_WINDOW_SECONDS = 60.0
_MAX_TRACKED_ADDRESSES = 1024
_MAX_BODY_BYTES = 16 * 1024


class JoinRateLimiter:
    """At most N join attempts per client address per minute.

    The join route is unauthenticated, so the addresses it sees are unbounded;
    once the map holds more than ``_MAX_TRACKED_ADDRESSES`` of them, every
    address whose attempts have all left the window is forgotten.
    """

    def __init__(self, attempts_per_minute: int = _JOIN_ATTEMPTS_PER_MINUTE) -> None:
        self._attempts_per_minute = attempts_per_minute
        self._attempt_times_by_address: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def tracked_address_count(self) -> int:
        with self._lock:
            return len(self._attempt_times_by_address)

    def allow(self, address: str, now_monotonic: float) -> bool:
        with self._lock:
            if len(self._attempt_times_by_address) > _MAX_TRACKED_ADDRESSES:
                self._forget_idle_addresses(now_monotonic)
            attempts = self._attempt_times_by_address.setdefault(address, deque())
            _drop_attempts_outside_window(attempts, now_monotonic)
            if len(attempts) >= self._attempts_per_minute:
                return False
            attempts.append(now_monotonic)
            return True

    def _forget_idle_addresses(self, now_monotonic: float) -> None:
        for address, attempts in list(self._attempt_times_by_address.items()):
            _drop_attempts_outside_window(attempts, now_monotonic)
            if not attempts:
                del self._attempt_times_by_address[address]


def _drop_attempts_outside_window(attempts: deque[float], now_monotonic: float) -> None:
    while attempts and now_monotonic - attempts[0] >= _JOIN_WINDOW_SECONDS:
        attempts.popleft()


class AgentAccessContext:
    """Everything the agent routes and the bearer admission need beyond the gateway's own state."""

    def __init__(
        self,
        paths: AgentAccessPaths,
        coordinates: WorkspaceCoordinates,
        repo_root: Path,
        environ: "os._Environ[str] | dict[str, str]",
        last_seen: LastSeenStore,
        skills_cache_path: Path,
        now: Callable[[], datetime],
    ) -> None:
        self.paths = paths
        self.coordinates = coordinates
        self.repo_root = repo_root
        self.environ = environ
        self.last_seen = last_seen
        self.skills_cache_path = skills_cache_path
        self.now = now
        self.join_rate_limiter = JoinRateLimiter()
        self.latchkey_summary = LatchkeySummaryCache()

    def grant_for_token(self, token: str) -> AgentGrant | None:
        """The unexpired grant a bearer token names; None for an unknown token. Raises AgentGrantsError when the file is malformed."""
        return find_grant_by_token(
            load_active_agent_grants(self.paths.grants_path, self.now()),
            token,
            self.now(),
        )


def _json(payload: object, status: int = 200) -> Response:
    return Response(json.dumps(payload), status=status, mimetype="application/json")


def _error(message: str, status: int) -> Response:
    return _json({"error": message}, status)


def _parse_json_body() -> dict[str, object] | None:
    """The request body as a JSON object, or None when it is not one or exceeds ``_MAX_BODY_BYTES``.

    The join route is unauthenticated, so the cap bounds the read itself rather
    than the length of a body already buffered.
    """
    if (request.content_length or 0) > _MAX_BODY_BYTES:
        return None
    raw = request.stream.read(_MAX_BODY_BYTES + 1)
    if len(raw) > _MAX_BODY_BYTES:
        return None
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        return None
    return parsed if is_json_object(parsed) else None


class _HumanCaller:
    """A session-authenticated caller: the owner, or a visitor whose grants say how far they reach."""

    def __init__(self, identity: RequesterIdentity, is_workspace_level: bool) -> None:
        self.identity = identity
        self.is_workspace_level = is_workspace_level

    @property
    def principal(self) -> str:
        return f"user:{self.identity.user_id}"


class _AgentCaller:
    """A bearer-authenticated caller."""

    def __init__(self, grant: AgentGrant) -> None:
        self.grant = grant

    @property
    def principal(self) -> str:
        return f"agent:{self.grant.agent_id}"

    @property
    def is_workspace_level(self) -> bool:
        return self.grant.is_workspace_scope


def register_agent_routes(
    app: Flask,
    context: AgentAccessContext,
    workspace_domain: str,
    auth_label: str,
    signing_secret: str,
    share_grants_path: Path,
    get_registered_apps: Callable[[], Sequence[RegisteredApp]],
) -> None:
    auth_origin = f"https://{auth_label}.{workspace_domain}"

    def _human_caller() -> _HumanCaller | None:
        identity = verify_session_from_cookies(signing_secret, request.cookies, workspace_domain)
        if identity is None:
            return None
        if identity.is_owner:
            return _HumanCaller(identity, is_workspace_level=True)
        try:
            grants = load_grants(share_grants_path)
        except GrantsError:
            return _HumanCaller(identity, is_workspace_level=False)
        return _HumanCaller(
            identity,
            is_workspace_level=grants.workspace.allows(identity.user_id, identity.email),
        )

    def _agent_caller() -> "_AgentCaller | Response | None":
        token = bearer_token_from_headers(request.headers.get("Authorization"))
        if token is None:
            return None
        try:
            grant = context.grant_for_token(token)
        except AgentGrantsError as exc:
            log(f"Agent access is misconfigured: {exc}")
            return _error("agent access is misconfigured", 503)
        if grant is None:
            return _error("this access token is not valid", 403)
        context.last_seen.touch(grant.agent_id, context.now(), time.monotonic())
        return _AgentCaller(grant)

    def _caller() -> "_HumanCaller | _AgentCaller | Response":
        agent = _agent_caller()
        if agent is not None:
            return agent
        human = _human_caller()
        if human is None:
            return _error("authentication required", 401)
        return human

    def _agent_only() -> "_AgentCaller | Response":
        """The bearer-authenticated caller of a route only agents may use, or the refusal to answer with."""
        agent = _agent_caller()
        if agent is None:
            return _error("authentication required", 401)
        return agent

    def _access_document(grant: AgentGrant, token: str | None) -> dict[str, object]:
        apps = get_registered_apps()
        document: dict[str, object] = {
            "agent_id": grant.agent_id,
            "name": grant.name,
            "scope": grant.scope,
            "granter": grant.granter,
            "expires_at": None if grant.expires_at is None else grant.expires_at.isoformat(),
            "workspace": {
                "domain": workspace_domain,
                "auth_origin": auth_origin,
                "host_id": context.coordinates.host_id,
                "workspace_id": context.coordinates.workspace_id,
                "display_name": context.coordinates.display_name,
            },
            "apps": app_origins_for(
                apps,
                workspace_domain,
                None if grant.is_workspace_scope else grant.app_names,
            ),
            "primer_url": f"{auth_origin}/_agent/primer",
            "helper": _helper_document(),
        }
        if token is not None:
            document["token"] = token
        if grant.is_workspace_scope:
            document["exec"] = {
                "origin": exec_origin_for(apps, workspace_domain),
                "audience": context.coordinates.exec_audience(workspace_domain),
                "host_public_key": context.coordinates.host_public_key,
            }
        return document

    def _helper_document() -> dict[str, str]:
        """The helper this workspace runs: its version, where the template repository publishes it, and its hash,
        so an agent can check the copy it installed is the one the workspace expects."""
        return {
            "version": template_ref(context.repo_root),
            "source_url": helper_source_url(context.repo_root),
            "sha256": file_sha256(HELPER_PATH),
        }

    @app.get("/_agent/helper")
    def helper() -> Response:
        try:
            source = HELPER_PATH.read_bytes()
        except OSError:
            return _error("the helper is not available on this workspace", 404)
        response = Response(source, mimetype="application/javascript")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Helper-Version"] = template_ref(context.repo_root)
        response.headers["X-Helper-SHA256"] = hashlib.sha256(source).hexdigest()
        return response

    @app.post("/_agent/join")
    def join_route() -> Response:
        address = forwarded_client_ip(request.headers) or request.remote_addr or "unknown"
        if not context.join_rate_limiter.allow(address, time.monotonic()):
            return _error("too many join attempts; try again in a minute", 429)
        body = _parse_json_body()
        if body is None:
            return _error("the body must be a JSON object", 400)
        code = body.get("code")
        public_key = body.get("public_key")
        name = body.get("name")
        if not isinstance(code, str) or not isinstance(public_key, str) or not isinstance(name, str):
            return _error("code, public_key, and name must be strings", 400)
        try:
            joined = join(context.paths, code, public_key, name, context.now())
        except JoinRefusedError as exc:
            if exc.status == 400:
                count_failed_join(context.paths, code, context.now())
            log(f"Refused a join from {address}: {exc}")
            return _error(str(exc), exc.status)
        except AgentGrantsError as exc:
            log(f"Agent access is misconfigured: {exc}")
            return _error("agent access is misconfigured", 503)
        log(f"Agent {joined.grant.agent_id} ({joined.grant.name}) joined with scope {joined.grant.scope}")
        return _json(_access_document(joined.grant, joined.token))

    @app.get("/_agent/me")
    def me() -> Response:
        caller = _agent_only()
        if isinstance(caller, Response):
            return caller
        return _json(_access_document(caller.grant, None))

    @app.get("/_agent/primer")
    def primer() -> Response:
        caller = _agent_only()
        if isinstance(caller, Response):
            return caller
        now = context.now()
        since_raw = request.args.get("since", "")
        since = parse_since(since_raw) or default_changes_since(now)
        skills = cached_skills_index(
            context.repo_root,
            context.skills_cache_path,
            template_ref(context.repo_root),
        )
        connected = (
            context.latchkey_summary.read(context.environ, time.monotonic())
            if caller.grant.is_workspace_scope
            else "Not applicable: this access reaches named apps only."
        )
        text = render_primer(
            repo_root=context.repo_root,
            scope=caller.grant.scope,
            workspace_display_name=context.coordinates.display_name,
            workspace_id=context.coordinates.workspace_id,
            skills_index=skills,
            apps=get_registered_apps(),
            workspace_domain=workspace_domain,
            connected_accounts=connected,
            changes=build_changes_feed(context.repo_root, since),
        )
        return Response(text, mimetype="text/markdown")

    @app.get("/_agent/skills")
    def skills() -> Response:
        caller = _agent_only()
        if isinstance(caller, Response):
            return caller
        return _json(
            cached_skills_index(
                context.repo_root,
                context.skills_cache_path,
                template_ref(context.repo_root),
            )
        )

    @app.get("/_agent/changes")
    def changes() -> Response:
        caller = _agent_only()
        if isinstance(caller, Response):
            return caller
        since = parse_since(request.args.get("since", ""))
        if since is None and request.args.get("since", ""):
            return _error("since must be an ISO 8601 timestamp", 400)
        return _json(build_changes_feed(context.repo_root, since or default_changes_since(context.now())))

    @app.post("/_agent/invites")
    def invites() -> Response:
        caller = _caller()
        if isinstance(caller, Response):
            return caller
        if not caller.is_workspace_level:
            return _error(
                "only someone with access to the whole workspace can invite an agent",
                403,
            )
        body = _parse_json_body()
        if body is None:
            return _error("the body must be a JSON object", 400)
        apps = get_registered_apps()
        try:
            scope = validate_scope(
                body.get("scope", "workspace"),
                sorted(build_grantable_service_names(apps)),
            )
        except AgentScopeError as exc:
            return _error(str(exc), 400)
        name_hint = body.get("name_hint", "")
        if not isinstance(name_hint, str):
            return _error("name_hint must be a string", 400)
        try:
            lifetime = grant_lifetime_from_days(body.get("expires_in_days"))
        except InvalidGrantLifetimeError as exc:
            return _error(str(exc), 400)
        try:
            minted = mint_invite(
                context.paths,
                scope,
                caller.principal,
                name_hint,
                lifetime,
                context.now(),
                via="gateway",
            )
        except AgentGrantsError as exc:
            log(f"Agent access is misconfigured: {exc}")
            return _error("agent access is misconfigured", 503)
        instructions = render_instruction_block(
            repo_root=context.repo_root,
            auth_origin=auth_origin,
            code=minted.code,
            expires_at=minted.invite.expires_at,
            scope=scope,
            workspace_display_name=context.coordinates.display_name,
            name_hint=minted.invite.name_hint,
            app_origins=app_origins_for(apps, workspace_domain, None if scope == "workspace" else list(scope)),
            helper_source_url=helper_source_url(context.repo_root),
            helper_sha256=file_sha256(HELPER_PATH),
        )
        return _json(
            {
                "code": minted.code,
                "expires_at": minted.invite.expires_at.isoformat(),
                "scope": scope,
                "granter": caller.principal,
                "instructions": instructions,
            }
        )

    @app.get("/_agent/grants")
    def grants_list() -> Response:
        caller = _caller()
        if isinstance(caller, Response):
            return caller
        try:
            grants = list_grants(context.paths, context.now())
        except AgentGrantsError as exc:
            log(f"Agent access is misconfigured: {exc}")
            return _error("agent access is misconfigured", 503)
        is_owner = isinstance(caller, _HumanCaller) and caller.identity.is_owner
        last_seen = context.last_seen.read_all()
        visible = visible_grants(grants, caller.principal, is_owner)
        return _json({"agents": [describe_grant(grant, last_seen.get(grant.agent_id)) for grant in visible]})

    @app.delete("/_agent/grants/<agent_id>")
    def grants_delete(agent_id: str) -> Response:
        caller = _caller()
        if isinstance(caller, Response):
            return caller
        if not is_agent_id(agent_id):
            return _error("not an agent id", 400)
        is_owner = isinstance(caller, _HumanCaller) and caller.identity.is_owner
        try:
            revoked = revoke(
                context.paths,
                agent_id,
                caller.principal,
                is_owner,
                context.now(),
                via="gateway",
            )
        except RevokeRefusedError as exc:
            return _error(str(exc), 404)
        except AgentGrantsError as exc:
            log(f"Agent access is misconfigured: {exc}")
            return _error("agent access is misconfigured", 503)
        return _json({"revoked": revoked.agent_id})
