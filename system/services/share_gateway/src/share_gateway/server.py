"""The gateway's HTTP surface: caddy's forward_auth backend + the login callback.

Every request to a shared origin passes through ``/_auth/verify`` (session
cookie verified, identity re-checked against the grants file, Origin policy
enforced, session cookie stripped from what the service sees, the verified
identity handed on as ``X-Imbue-Identity``). A visiting agent presents a bearer
admission token instead of a cookie; ``admit_agent`` resolves it against the
agent grants and the agent's scope, and the identity handed on names the agent
rather than an account. Visitors without a session are
bounced to the accounts broker and land back on ``/_auth/callback``, which
verifies the broker's handoff token and sets the workspace session cookie. A
callback whose nonce or token no longer verifies (a reopened link, a slow
redirect) heals itself: a visitor who already holds a session is sent on, and
anyone else is sent through the broker once more; only a second failure shows
the "Sign-in link expired" page.

``GET /connect-info`` is the one route for the Screen service alone: it lives on the
loopback port caddy never routes a visitor to (caddy reaches this app only for
``/_auth/*``, ``/_agent/*`` and ``/_health``), and answers the relay credential and the media candidates.
"""

import base64
import hashlib
import html
import json
import secrets
import threading
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlencode
from urllib.parse import urlsplit

from flask import Flask
from flask import Response
from flask import request
from share_grants.grants import Grants
from share_grants.grants import GrantsError
from share_grants.grants import load_grants
from share_grants.grants import upgrade_invites_in_file
from werkzeug.datastructures import Headers

from share_gateway.caddyfile import RegisteredApp
from share_gateway.caddyfile import build_grantable_service_names
from share_gateway.caddyfile import build_label_to_name
from share_gateway.caddyfile import is_started_on_demand
from share_gateway.handoff import HandoffVerificationError
from share_gateway.handoff import JwksCache
from share_gateway.handoff import SingleUseJtiRegistry
from share_gateway.handoff import verify_handoff_token
from share_gateway.hostnames import service_for_host
from share_gateway.hostnames import workspace_origins_allow
from share_gateway.identity import AgentIdentity
from share_gateway.identity import IDENTITY_HEADER
from share_gateway.identity import RequesterIdentity
from share_gateway.identity import bearer_token_from_headers
from share_gateway.identity import render_agent_identity_header
from share_gateway.identity import render_identity_header
from share_gateway.log import log
from share_gateway.materials import ShareMaterials
from share_gateway.origin_policy import is_request_origin_allowed
from share_gateway.request_counts import DENIED_AGENT_ACCESS_UNCONFIGURED
from share_gateway.request_counts import DENIED_AGENT_TOKEN
from share_gateway.request_counts import DENIED_FOREIGN_HOST
from share_gateway.request_counts import DENIED_GRANTS_MISCONFIGURED
from share_gateway.request_counts import DENIED_NO_GRANT
from share_gateway.request_counts import DENIED_NO_SESSION
from share_gateway.request_counts import DENIED_ORIGIN
from share_gateway.request_counts import OUTCOME_ALLOWED
from share_gateway.request_counts import REQUESTER_KIND_AGENT
from share_gateway.request_counts import REQUESTER_KIND_NONE
from share_gateway.request_counts import REQUESTER_KIND_OWNER
from share_gateway.request_counts import REQUESTER_KIND_USER
from share_gateway.request_counts import RequestCounter
from share_gateway.request_counts import classify_method
from share_gateway.session_cookie import clear_session_cookie
from share_gateway.session_cookie import mint_session_cookie_value
from share_gateway.session_cookie import set_session_cookie
from share_gateway.session_cookie import strip_session_cookie
from share_gateway.session_cookie import verify_session_from_cookies

_PENDING_LOGIN_TTL_SECONDS = 600.0

# The workspace shell service name; used to report backend readiness in the
# authenticated /_health detail.
_SYSTEM_INTERFACE_SERVICE_NAME = "system_interface"

# The workspace shell service; its label origin is the safe post-login landing
# spot when a visitor's ``next`` cannot be honored (the bare domain no longer
# routes).
_SHELL_SERVICE_NAME = "system_interface"

# The stylesheet the gateway's own pages inline; the page policy admits it by hash.
_PAGE_STYLE = (
    "body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;"
    "height:100vh;margin:0;color:#334155}div{text-align:center;max-width:32rem;padding:0 1rem}"
    ".button{display:inline-block;margin-top:0.5rem;padding:0.5rem 1rem;border-radius:0.375rem;"
    "background:#334155;color:#fff;text-decoration:none;border:1px solid #334155;font:inherit;cursor:pointer}"
    ".button.secondary{background:#fff;color:#334155;margin-left:0.5rem}form{display:inline}"
)
_PAGE_STYLE_HASH = base64.b64encode(hashlib.sha256(_PAGE_STYLE.encode("utf-8")).digest()).decode("ascii")


def page_content_security_policy(auth_origin: str, broker_origin: str) -> str:
    """The gateway's own pages load nothing but their inline stylesheet, and their one form (switching account)
    posts to the auth origin, which redirects to the broker: a browser holds that redirect to form-action too.
    Stated in full because the frame-ancestors policy caddy adds governs embedding only; the two headers compose by
    intersection."""
    return (
        f"default-src 'none'; style-src 'sha256-{_PAGE_STYLE_HASH}'; base-uri 'none'; "
        f"form-action {auth_origin} {broker_origin}"
    )


def _render_page(
    title: str, body_html: str, refresh_seconds: int | None = None, referrer_policy: str | None = None
) -> str:
    """One of the gateway's own full-page answers: a heading and a few lines, centred. ``referrer_policy`` replaces
    the one caddy's header gives the page."""
    refresh = f'<meta http-equiv="refresh" content="{refresh_seconds}">' if refresh_seconds is not None else ""
    referrer = f'<meta name="referrer" content="{referrer_policy}">' if referrer_policy is not None else ""
    return (
        f'<!DOCTYPE html>\n<html><head><meta charset="utf-8">{refresh}{referrer}\n'
        f"<title>{html.escape(title)}</title>\n<style>{_PAGE_STYLE}</style></head>\n"
        f"<body><div>{body_html}</div></body></html>\n"
    )


_LOADING_PAGE = _render_page(
    "Loading...",
    "<h1>Starting up&hellip;</h1>\n<p>This service is not ready yet. The page retries automatically.</p>",
    refresh_seconds=3,
)

# A press reloads the address the visitor was opening: the gateway's pages are served in place of
# an app's own page, at that page's address.
_TRY_AGAIN_BUTTON = '<a class="button" href="">Try again</a>'

_UNRESPONSIVE_WAITING_PAGE = _render_page(
    "Waiting for this page",
    "<h1>Waiting for this page</h1>\n<p>This page isn't responding yet. It will open as soon as it answers.</p>",
    refresh_seconds=3,
)

_UNRESPONSIVE_PAGE = _render_page(
    "This page isn't responding",
    "<h1>This page isn't responding</h1>\n"
    "<p>This page isn't responding right now. Let the person who shared it know, then try again.</p>\n"
    f"{_TRY_AGAIN_BUTTON}",
)

_UNRESPONSIVE_NOT_RESTARTED_PAGE = _render_page(
    "This app isn't responding",
    "<h1>This app isn't responding</h1>\n"
    "<p>This app isn't responding. The workspace owner may need to restart it.</p>\n"
    f"{_TRY_AGAIN_BUTTON}",
)

_NOTHING_SHARED_HERE_PAGE = _render_page(
    "Nothing shared here",
    "<h1>Nothing shared here</h1>\n"
    "<p>Nothing is shared at this address right now. Check the link with the person who sent it.</p>",
)


def _origin_refused_page(open_url: str) -> str:
    """A request sent from another site; ``open_url`` opens the page itself with a plain GET, which is always
    allowed ("" when this page is served at that page's own address)."""
    return _render_page(
        "This page can't be opened from another site",
        "<h1>This page can't be opened from another site</h1>\n"
        "<p>For your security, this workspace doesn't accept requests sent from other websites. "
        "Open the page directly instead.</p>\n"
        f'<a class="button" href="{html.escape(open_url)}">Open this page</a>',
    )


_EXPIRED_LOGIN_PAGE = _render_page(
    "Sign-in link expired",
    "<h1>Sign-in link expired</h1>\n"
    "<p>This sign-in link was already used or has expired. Open the workspace link again.</p>",
)

_MISCONFIGURED_PAGE = _render_page(
    "Sharing is misconfigured",
    "<h1>Sharing is misconfigured</h1>\n"
    "<p>This workspace's sharing settings could not be read, so nobody can be let in right now.\n"
    "The workspace owner needs to fix the workspace's sharing settings.</p>",
)

# How long an address may go unanswered before the not-responding page stops retrying, and how
# long a pause between its requests ends one wait and starts the next. The pause outlasts both the
# page's own 3-second reload and the shell's 15-second could-not-start page, which a visitor
# alternates with while the shell tries to start an app.
_UNRESPONSIVE_GIVE_UP_SECONDS = 60.0
_UNRESPONSIVE_WAIT_GAP_SECONDS = 30.0


def _not_shared_with_account_page(reload_url: str, switch_account_url: str, next_url: str) -> str:
    """The refusal of a signed-in account; ``reload_url`` is the page the visitor was opening ("" when this page is
    served at that page's own address)."""
    return _render_page(
        "Not shared with you",
        "<h1>Not shared with you</h1>\n"
        "<p>This page hasn't been shared with your account. "
        "Ask the person who shared it to give you access, then reload this page.</p>\n"
        f'<a class="button" href="{html.escape(reload_url)}">Reload</a>'
        # A form, so only a press on this page can end the session: a link could be followed from any site.
        f'<form method="post" action="{html.escape(switch_account_url)}">'
        f'<input type="hidden" name="next" value="{html.escape(next_url)}">'
        '<button class="button secondary" type="submit">Sign in with a different account</button></form>',
        # Served at an app's origin, the form posts to the auth origin: under caddy's same-origin policy the browser
        # would send that post with "Origin: null", which switch-account refuses.
        referrer_policy="strict-origin",
    )


class PendingLogin:
    """One in-flight broker login: when its nonce was minted, and whether it is already the retry of a failed callback."""

    def __init__(self, created_at: float, is_retry: bool) -> None:
        self.created_at = created_at
        self.is_retry = is_retry


class PendingLoginRegistry:
    """Nonces for in-flight broker logins: minted at redirect time, consumed once at the callback."""

    def __init__(self) -> None:
        self._pending_login_by_nonce: dict[str, PendingLogin] = {}
        self._lock = threading.Lock()

    def mint(self, is_retry: bool) -> str:
        # A retry nonce marks the one self-heal round trip a failed callback
        # gets: when the callback that consumes it fails too, the visitor sees
        # the expired-link page instead of being bounced to the broker again.
        nonce = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._pending_login_by_nonce = {
                pending_nonce: pending_login
                for pending_nonce, pending_login in self._pending_login_by_nonce.items()
                if now - pending_login.created_at < _PENDING_LOGIN_TTL_SECONDS
            }
            self._pending_login_by_nonce[nonce] = PendingLogin(created_at=now, is_retry=is_retry)
        return nonce

    def consume(self, nonce: str) -> PendingLogin | None:
        """Take the pending login for ``nonce`` (single-use); None when unknown, already consumed, or expired."""
        with self._lock:
            pending_login = self._pending_login_by_nonce.pop(nonce, None)
        if pending_login is None or time.monotonic() - pending_login.created_at >= _PENDING_LOGIN_TTL_SECONDS:
            return None
        return pending_login


class UnansweredAddressWaits:
    """How long each shared address has gone without its app answering, across every visitor.

    Each request caddy hands the gateway because an app's backend would not take the connection
    counts as one more moment of the same wait, unless the last one was long enough ago that the
    app may have answered in between, which starts a new wait.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._first_and_last_by_address: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def record_unanswered_request(self, address: str) -> float:
        """Record one unanswered request for ``address``; answer how long its current wait has lasted."""
        now = self._clock()
        with self._lock:
            first, last = self._first_and_last_by_address.get(address, (now, now))
            if now - last > _UNRESPONSIVE_WAIT_GAP_SECONDS:
                first = now
            # A wait whose address has gone quiet is over, so it is dropped rather than kept for good.
            self._first_and_last_by_address = {
                other: times
                for other, times in self._first_and_last_by_address.items()
                if now - times[1] <= _UNRESPONSIVE_WAIT_GAP_SECONDS
            }
            self._first_and_last_by_address[address] = (first, now)
        return now - first


def _is_html_navigation(method: str, accept_header: str, is_websocket_upgrade: bool) -> bool:
    return method.upper() == "GET" and not is_websocket_upgrade and "text/html" in accept_header.lower()


def forwarded_client_ip(headers: Headers | Mapping[str, str]) -> str:
    """The real client address of the request being verified, or '' when unknown.

    frpc stamps each spliced connection with PROXY protocol, so caddy's
    X-Forwarded-For on the auth subrequest carries the address the relay saw
    (the visitor or scanner), not frpc's loopback. The first entry is the
    client; later entries would be intermediaries appending.
    """
    forwarded_for = headers.get("X-Forwarded-For", "")
    return forwarded_for.split(",")[0].strip() if forwarded_for else ""


def _requested_url(host: str, forwarded_uri: str) -> str:
    uri = forwarded_uri if forwarded_uri.startswith("/") else "/"
    return f"https://{host}{uri}"


def build_gateway_app(
    materials: ShareMaterials,
    grants_path: Path,
    signing_secret: str,
    jwks_cache: JwksCache,
    jti_registry: SingleUseJtiRegistry,
    pending_logins: PendingLoginRegistry,
    unanswered_waits: UnansweredAddressWaits,
    auth_label: str,
    # Reads the registered services (from apps.toml) fresh on each call, so a
    # service registered while shared is recognized without rebuilding the app.
    # Grants are keyed by service name, not label.
    get_registered_apps: Callable[[], Sequence[RegisteredApp]],
    # The current relay credential and media candidates for the Screen service (share_gateway.connect_info),
    # read fresh on each call so an assignment re-poll or a credential refresh shows at once.
    get_connect_info: Callable[[], dict[str, object]],
    # Resolves a bearer admission token to the visiting agent's identity when
    # its scope admits the named service; None answers 403. Absent (older
    # callers, tests of the human path), every bearer request is refused.
    admit_agent: Callable[[str, str | None], AgentIdentity | None] | None = None,
    # Counts every request the verify handler answers (request_counts.py); None counts nothing (tests of the
    # routes alone).
    request_counter: RequestCounter | None = None,
) -> Flask:
    app = Flask(__name__)
    workspace_domain = materials.workspace_domain
    auth_origin = f"https://{auth_label}.{workspace_domain}"
    chrome_origin = materials.chrome_origin

    auth_host = f"{auth_label}.{workspace_domain}"
    broker_parts = urlsplit(materials.broker_url)
    page_policy = page_content_security_policy(auth_origin, f"{broker_parts.scheme}://{broker_parts.netloc}")

    def _html_page(page: str, status: int) -> Response:
        """One of the gateway's own pages, under the policy that admits exactly its contents."""
        return Response(page, status=status, mimetype="text/html", headers={"Content-Security-Policy": page_policy})

    def _nothing_shared_here() -> Response:
        return _html_page(_NOTHING_SHARED_HERE_PAGE, status=403)

    def _origin_refused(open_url: str) -> Response:
        return _html_page(_origin_refused_page(open_url), status=403)

    def _not_shared_with_account(requested_url: str, reload_url: str) -> Response:
        next_url = requested_url if _is_workspace_url(requested_url) else ""
        switch_account_url = f"{auth_origin}/_auth/switch-account"
        return _html_page(_not_shared_with_account_page(reload_url, switch_account_url, next_url), status=403)

    def _expired_login() -> Response:
        return _html_page(_EXPIRED_LOGIN_PAGE, status=403)

    def _misconfigured() -> Response:
        return _html_page(_MISCONFIGURED_PAGE, status=503)

    def _apply_health_cors(response: Response) -> Response:
        """Echo the hosted-chrome origin so it can probe /_health with credentials.

        Only the configured chrome origin is allowed, and only for the health
        probe; every other cross-origin fetch is left to the browser's default
        (no header = blocked). Credentialed CORS forbids the ``*`` wildcard, so
        the exact origin is echoed and ``Vary: Origin`` keeps caches honest.
        """
        request_origin = request.headers.get("Origin", "")
        if chrome_origin and request_origin == chrome_origin:
            response.headers["Access-Control-Allow-Origin"] = chrome_origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        return response

    # Denials are the traffic worth seeing (scanner probes, revoked visitors,
    # unknown hostnames); allowed requests stay unlogged. The client address
    # is the real one the relay saw, via frpc's PROXY protocol stamp.
    def _log_denied(reason: str, host: str) -> None:
        client_ip = forwarded_client_ip(request.headers) or "unknown-client"
        log(f"Denied {client_ip} -> {host or '(no host)'}: {reason}")

    @app.get("/_auth/healthz")
    def healthz() -> Response:
        return Response("ok", status=200, mimetype="text/plain")

    @app.route("/_health", methods=["GET", "OPTIONS"])
    def health() -> Response:
        # Reachable at every workspace origin (routed site-wide in caddy) so the
        # hosted chrome can probe a workspace it has an origin for. A CORS
        # preflight is answered outright.
        if request.method == "OPTIONS":
            preflight = Response(status=204)
            preflight.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            return _apply_health_cors(preflight)
        identity = verify_session_from_cookies(signing_secret, request.cookies, workspace_domain)
        if identity is None:
            # Bare liveness: the gateway (hence the tunnel) is up. No detail
            # without a session -- the workspace host id is a capability, but
            # backend/service state is not leaked to an unauthenticated probe.
            return _apply_health_cors(Response(status=204))
        label_to_name = build_label_to_name(get_registered_apps())
        is_backend_registered = _SYSTEM_INTERFACE_SERVICE_NAME in label_to_name.values()
        detail = {
            "gateway": "ok",
            "backend": "ok" if is_backend_registered else "starting",
            "owner": identity.is_owner,
        }
        if identity.is_owner:
            # The owner's chrome needs the service origins (unguessable labels)
            # to reach individual services -- most importantly owner-exec. Only
            # the owner gets the map; a visitor sees just liveness + backend.
            detail["services"] = {name: label for label, name in label_to_name.items()}
        return _apply_health_cors(Response(_json_body(detail), mimetype="application/json"))

    @app.get("/connect-info")
    def connect_info() -> Response:
        # A relay credential is a bearer token: nothing may cache it on the way to the Screen service.
        return Response(
            _json_body(get_connect_info()), mimetype="application/json", headers={"Cache-Control": "no-store"}
        )

    @app.get("/_auth/loading")
    def loading() -> Response:
        return _html_page(_LOADING_PAGE, status=503)

    # Caddy rewrites the path of whatever request the backend refused and keeps its method, so a page's POST to a
    # down app is answered as not responding too, rather than as a method this route does not take.
    @app.route("/_auth/unresponsive", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    def unresponsive() -> Response:
        # Caddy rewrites the path but keeps the Host, so the address the visitor was opening is
        # still known here. Read raw: werkzeug's parsed host is empty for a label with an
        # underscore, which the shell's has.
        host = request.headers.get("Host", "")
        registered_apps = get_registered_apps()
        _is_ours, service_name = service_for_host(
            host, workspace_domain, build_label_to_name(registered_apps), auth_label
        )
        # Counted per service rather than per spelling of its host (case, a port), which caddy passes on as sent.
        address = service_name if service_name is not None else host.strip().lower()
        if unanswered_waits.record_unanswered_request(address) < _UNRESPONSIVE_GIVE_UP_SECONDS:
            return _html_page(_UNRESPONSIVE_WAITING_PAGE, status=503)
        app_entry = next((entry for entry in registered_apps if entry.name == service_name), None)
        if app_entry is not None and not is_started_on_demand(app_entry, registered_apps):
            return _html_page(_UNRESPONSIVE_NOT_RESTARTED_PAGE, status=503)
        return _html_page(_UNRESPONSIVE_PAGE, status=503)

    @app.post("/_auth/switch-account")
    def switch_account() -> Response:
        # Ending the gateway's session alone would sign the visitor straight back in as the same
        # account: the broker remembers it. Sending them through the broker unconfirmed makes it
        # ask which account to continue as, with a way to use a different one.
        next_url = request.form.get("next", "")
        safe_next = next_url if _is_workspace_url(next_url) else ""
        # Only the not-granted page, on one of this workspace's origins, may end the session.
        origin_header = request.headers.get("Origin", "")
        if not origin_header or not workspace_origins_allow(
            origin_header, workspace_domain, build_label_to_name(get_registered_apps()), auth_label
        ):
            _log_denied("switch-account posted without one of this workspace's origins", auth_host)
            return _origin_refused(_post_login_redirect_target(safe_next))
        response = _redirect_to_broker(safe_next, is_confirmed=False, is_retry=False)
        response.status_code = 303
        clear_session_cookie(response, workspace_domain)
        return response

    @app.get("/_auth/verify")
    def verify() -> Response:
        host = request.headers.get("X-Forwarded-Host", "")
        method = request.headers.get("X-Forwarded-Method", "GET")
        forwarded_uri = request.headers.get("X-Forwarded-Uri", "/")
        registered_apps = get_registered_apps()
        label_to_name = build_label_to_name(registered_apps)
        is_ours, service_name = service_for_host(host, workspace_domain, label_to_name, auth_label)

        # Upgrade is hop-by-hop, so caddy passes it explicitly (see the
        # forward_auth block in the rendered Caddyfile).
        is_websocket_upgrade = request.headers.get("X-Forwarded-Upgrade", "").lower() == "websocket"
        method_class = classify_method(method, is_websocket_upgrade)

        def _counted(response: Response, requester_kind: str, requester_id: str, outcome: str) -> Response:
            if request_counter is not None:
                request_counter.record(service_name or "", requester_kind, requester_id, method_class, outcome)
            return response

        if not is_ours:
            _log_denied("hostname is not one of this workspace's origins", host)
            return _counted(_nothing_shared_here(), REQUESTER_KIND_NONE, "", DENIED_FOREIGN_HOST)

        origin_header = request.headers.get("Origin")
        bearer_token = bearer_token_from_headers(request.headers.get("Authorization"))
        if bearer_token is None and not is_request_origin_allowed(
            method,
            origin_header,
            is_websocket_upgrade,
            workspace_domain,
            label_to_name,
            auth_label,
            chrome_origin,
        ):
            _log_denied("request Origin is not allowed", host)
            return _counted(_origin_refused(""), REQUESTER_KIND_NONE, "", DENIED_ORIGIN)

        # A CORS preflight from the hosted chrome must reach the service so it
        # can answer with its CORS headers -- and preflights never carry
        # credentials, so the session check below would bounce them. Allowing
        # the preflight through exposes nothing: the actual request that
        # follows it is session-checked as usual.
        if method.upper() == "OPTIONS" and chrome_origin and origin_header == chrome_origin:
            return _counted(Response(status=200), REQUESTER_KIND_NONE, "", OUTCOME_ALLOWED)

        cookie_header = request.headers.get("Cookie", "")

        # A visiting agent presents its admission token as a bearer header. It
        # is never ambient, so the Origin policy above is moot for it (the
        # policy guards browsers' automatically attached cookies); the token
        # alone is checked, against the agent grants rather than the human
        # ones, and the agent's scope decides which origins it may reach.
        if bearer_token is not None:
            if admit_agent is None:
                _log_denied("bearer token presented but agent access is not configured", host)
                return _counted(
                    Response("authentication required", status=401, mimetype="text/plain"),
                    REQUESTER_KIND_NONE,
                    "",
                    DENIED_AGENT_ACCESS_UNCONFIGURED,
                )
            agent_identity = admit_agent(bearer_token, service_name)
            if agent_identity is None:
                _log_denied("bearer token is not a valid admission token for this origin", host)
                return _counted(
                    Response("forbidden", status=403, mimetype="text/plain"),
                    REQUESTER_KIND_NONE,
                    "",
                    DENIED_AGENT_TOKEN,
                )
            return _counted(
                Response(
                    status=200,
                    headers={
                        "X-Share-Filtered-Cookie": strip_session_cookie(cookie_header),
                        IDENTITY_HEADER: render_agent_identity_header(agent_identity),
                    },
                ),
                REQUESTER_KIND_AGENT,
                agent_identity.agent_id,
                OUTCOME_ALLOWED,
            )

        identity = verify_session_from_cookies(signing_secret, request.cookies, workspace_domain)
        if identity is None:
            accept_header = request.headers.get("Accept", "")
            if _is_html_navigation(method, accept_header, is_websocket_upgrade):
                return _counted(
                    _redirect_to_broker(_requested_url(host, forwarded_uri), is_confirmed=False, is_retry=False),
                    REQUESTER_KIND_NONE,
                    "",
                    DENIED_NO_SESSION,
                )
            _log_denied("no session on a non-HTML request", host)
            return _counted(
                Response("authentication required", status=401, mimetype="text/plain"),
                REQUESTER_KIND_NONE,
                "",
                DENIED_NO_SESSION,
            )

        requester_kind = REQUESTER_KIND_OWNER if identity.is_owner else REQUESTER_KIND_USER
        requester_id = "" if identity.is_owner else identity.user_id
        # The owner reaches every origin of their own workspace regardless of
        # the grants file (the broker vouched for ownership by user id). A
        # non-owner is re-checked against the grants file on every request so
        # revocation is instant; a malformed or missing grants file fails closed.
        if not identity.is_owner:
            try:
                grants = load_grants(grants_path)
            except GrantsError as exc:
                _log_denied(f"grants file is missing or malformed (failing closed): {exc}", host)
                return _counted(_misconfigured(), requester_kind, requester_id, DENIED_GRANTS_MISCONFIGURED)
            if not grants.allows(
                identity.user_id, identity.email, service_name, build_grantable_service_names(registered_apps)
            ):
                _log_denied("session identity has no grant admitting it to this service", host)
                return _counted(
                    # Caddy answers the visitor with this page at the address they were opening.
                    _not_shared_with_account(_requested_url(host, forwarded_uri), reload_url=""),
                    requester_kind,
                    requester_id,
                    DENIED_NO_GRANT,
                )
            _upgrade_invite(grants, identity)

        # Expose the caller's identity so caddy can copy it to the backend. It is
        # authoritative because the gateway sets it after verifying the signed
        # session, and caddy strips any inbound copy before the forward_auth
        # subrequest.
        response_headers = {
            "X-Share-Filtered-Cookie": strip_session_cookie(cookie_header),
            IDENTITY_HEADER: render_identity_header(identity),
        }
        return _counted(Response(status=200, headers=response_headers), requester_kind, requester_id, OUTCOME_ALLOWED)

    @app.get("/_auth/callback")
    def callback() -> Response:
        token = request.args.get("token", "")
        state = request.args.get("state", "")
        next_url = request.args.get("next", "")
        pending_login = pending_logins.consume(state) if state else None
        is_retry = pending_login is not None and pending_login.is_retry
        if pending_login is None:
            return _recover_from_failed_handoff(
                "callback nonce is unknown, already used, or expired", next_url, is_retry
            )
        if not token:
            return _recover_from_failed_handoff("callback carries no handoff token", next_url, is_retry)
        try:
            handoff = verify_handoff_token(
                token=token,
                expected_nonce=state,
                workspace_domain=workspace_domain,
                jwks_cache=jwks_cache,
                jti_registry=jti_registry,
            )
        except HandoffVerificationError as exc:
            return _recover_from_failed_handoff(f"handoff token failed verification: {exc}", next_url, is_retry)
        try:
            grants = load_grants(grants_path)
        except GrantsError as exc:
            _log_denied(f"grants file is missing or malformed (failing closed): {exc}", auth_host)
            return _misconfigured()
        # The owner always has access regardless of the grants file (the broker
        # vouched for ownership by user id, so an owner never needs an explicit
        # grant to reach their own workspace). Non-owners still need a grant.
        if not handoff.is_owner and not grants.allows_any(
            handoff.user_id, handoff.email, build_grantable_service_names(get_registered_apps())
        ):
            _log_denied(
                f"signed-in account {handoff.user_id} has no grant admitting it anywhere on this workspace", auth_host
            )
            # Served at the single-use callback address, so Reload goes to the page the visitor was opening.
            return _not_shared_with_account(next_url, reload_url=_post_login_redirect_target(next_url))
        _upgrade_invite(grants, handoff)

        response = Response(status=302, headers={"Location": _post_login_redirect_target(next_url)})
        set_session_cookie(
            response, mint_session_cookie_value(signing_secret, handoff, workspace_domain), workspace_domain
        )
        return response

    def _recover_from_failed_handoff(reason: str, next_url: str, is_retry: bool) -> Response:
        # A callback that cannot be verified is usually benign -- a reopened
        # link, a back button, a redirect that outlived the 60-second token --
        # so rather than a dead end: a visitor who already holds a session is
        # simply sent on, and anyone else gets exactly one more trip through
        # the broker (on a retry nonce). A retry that fails again is the end.
        _log_denied(reason, auth_host)
        if verify_session_from_cookies(signing_secret, request.cookies, workspace_domain) is not None:
            return Response(status=302, headers={"Location": _post_login_redirect_target(next_url)})
        if is_retry:
            return _expired_login()
        return _redirect_to_broker(next_url if _is_workspace_url(next_url) else "", is_confirmed=True, is_retry=True)

    def _post_login_redirect_target(next_url: str) -> str:
        # Bounce onward to the origin the visitor was reaching, but only if it
        # is genuinely one of this workspace's own origins. The bare domain no
        # longer routes, so the safe fallback is the shell (system_interface)
        # label origin; failing that, the workspace's own shell service simply
        # is not registered and there is nowhere sensible to land.
        if _is_workspace_url(next_url):
            return next_url
        label_to_name = build_label_to_name(get_registered_apps())
        shell_label = next((label for label, name in label_to_name.items() if name == _SHELL_SERVICE_NAME), None)
        return f"https://{shell_label}.{workspace_domain}/" if shell_label else auth_origin

    def _redirect_to_broker(next_url: str, is_confirmed: bool, is_retry: bool) -> Response:
        nonce = pending_logins.mint(is_retry=is_retry)
        # The broker delivers its post-login callback to the dedicated auth
        # origin (the only label serving /_auth/callback), then bounces to
        # ``next`` (the origin the visitor was actually reaching).
        query: dict[str, str] = {
            "machine_domain": workspace_domain,
            "next": next_url,
            "callback_origin": auth_origin,
            "state": nonce,
        }
        if is_confirmed:
            query["confirmed"] = "1"
        return Response(status=302, headers={"Location": f"{materials.broker_url}/share/authorize?{urlencode(query)}"})

    def _is_workspace_url(url: str) -> bool:
        if not url.startswith("https://"):
            return False
        next_host = url.removeprefix("https://").split("/", 1)[0]
        is_ours, _service = service_for_host(
            next_host, workspace_domain, build_label_to_name(get_registered_apps()), auth_label
        )
        return is_ours

    def _upgrade_invite(grants: Grants, identity: RequesterIdentity) -> None:
        # An ``emails`` entry is an invitation; once its holder has visited, the
        # grant is rewritten to their user id so a later email change on their
        # account cannot revoke it. Best effort: a failed rewrite leaves the
        # invitation in place, and the next visit tries again.
        if identity.is_owner or not grants.has_email_invite(identity.email):
            return
        try:
            is_upgraded = upgrade_invites_in_file(grants_path, identity.email, identity.user_id)
        except (GrantsError, OSError) as exc:
            log(f"Could not upgrade the invitation for {identity.email} to a user grant: {exc}")
            return
        if is_upgraded:
            log(f"Upgraded the invitation for {identity.email} to a grant for user {identity.user_id}")

    return app


def _json_body(payload: dict[str, object]) -> str:
    return json.dumps(payload)
