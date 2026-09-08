"""Accounts broker for share login handoff (/share/*).

Served at the accounts domain (Modal custom domain; dev tiers use the plain
connector URL). A visitor hitting a shared workspace without a session is
302'd here by the workspace gateway; the broker resolves the same
SuperTokens browser session the hosted accounts surface owns
(``accounts_web``), shows a one-click "Continue as ..." confirmation, then
mints a 60-second RS256 handoff JWT audience-bound to that one workspace
domain and redirects to the gateway's callback, which verifies it against
the JWKS published below.

The login page itself lives on the merged accounts surface (``/login``);
this module only owns the share-specific authorization step and the JWKS.
Satisfying a share grant is the one place a *visitor's* email is an
authorization identity, so it requires a verified email -- an unverified
session gets the verification email (contextual send) and a check-your-inbox
page instead of a handoff.
"""

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlparse

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError

import imbue.remote_service_connector.accounts_web as accounts_web_module
import imbue.remote_service_connector.auth as auth_module
import imbue.remote_service_connector.auth_proxy as auth_proxy_module
import imbue.remote_service_connector.shares as shares_module
from imbue.modal_app_kit.log_format import deployed_minds_env_name
from imbue.modal_app_kit.metrics import emit_metric
from imbue.modal_app_kit.request_logging import ensure_info_log_handler
from imbue.remote_service_connector.http_api import handle_endpoint_errors

logger = logging.getLogger(__name__)

# Dedicated logger for the append-only share-visit records: one JSON line per
# authorized visit, parsed downstream by the analytics aggregation (the
# ``share_tunnel_logins`` table only keeps a last-login upsert, so these lines
# are the visit *history*). INFO must actually reach stderr, hence the
# explicit handler.
_share_visit_logger = logging.getLogger(f"{__name__}.visits")
ensure_info_log_handler(_share_visit_logger)


def _log_share_visit_authorized(
    visitor_user_id: str, host_id: str, owner_share_label: str, workspace_domain: str, is_owner: bool
) -> None:
    record = {
        "type": "share_visit_authorized",
        "visitor_user_id": visitor_user_id,
        "host_id": host_id,
        "owner_share_label": owner_share_label,
        "workspace_domain": workspace_domain,
        "is_owner": is_owner,
    }
    # Same env stamp as the access-log lines, so the analytics aggregation can
    # filter one env's records out of the shared per-tier log store.
    env_name = deployed_minds_env_name()
    if env_name:
        record["minds_env"] = env_name
    _share_visit_logger.info("%s", json.dumps(record, ensure_ascii=True, separators=(",", ":")))


router = APIRouter()

_BROKER_HANDOFF_TOKEN_TTL_SECONDS = 60
_BROKER_HANDOFF_ALGORITHM = "RS256"


def _broker_signing_key() -> rsa.RSAPrivateKey:
    """The broker's RS256 signing key (shared with the accounts surface's OAuth state)."""
    return accounts_web_module.accounts_signing_key()


def _broker_key_id(public_key: rsa.RSAPublicKey) -> str:
    """A stable key id derived from the public key, published in the JWKS and stamped into token headers."""
    spki_der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(spki_der).hexdigest()[:16]


def _base64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_broker_jwks(public_key: rsa.RSAPublicKey) -> dict[str, Any]:
    """The JWKS document workspace gateways verify handoff tokens against."""
    numbers = public_key.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": _BROKER_HANDOFF_ALGORITHM,
                "kid": _broker_key_id(public_key),
                "n": _base64url_uint(numbers.n),
                "e": _base64url_uint(numbers.e),
            }
        ]
    }


def mint_share_handoff_token(
    signing_key: rsa.RSAPrivateKey,
    user_id: str,
    email: str,
    machine_domain: str,
    nonce: str,
    is_owner: bool,
) -> str:
    """Mint the 60-second, single-audience JWT the gateway's callback consumes.

    The ``owner`` claim lets the gateway admit the workspace's owner to every
    origin regardless of the grants file (and skip the verified-email gate).
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "owner": is_owner,
        "aud": machine_domain,
        "jti": secrets.token_urlsafe(16),
        "nonce": nonce,
        "iat": now,
        "exp": now + timedelta(seconds=_BROKER_HANDOFF_TOKEN_TTL_SECONDS),
    }
    return pyjwt.encode(
        payload,
        signing_key,
        algorithm=_BROKER_HANDOFF_ALGORITHM,
        headers={"kid": _broker_key_id(signing_key.public_key())},
    )


def _is_url_under_domain(url: str, machine_domain: str) -> bool:
    """Whether ``url`` is an https URL whose host is (a subdomain of) ``machine_domain``.

    Carrying a host in a redirect target is an open-redirect surface, so any
    host-bearing value the broker forwards a signed token toward -- the
    callback origin, the post-login ``next`` -- is checked against the share's
    own domain before use.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    domain = machine_domain.lower()
    return host == domain or host.endswith("." + domain)


def _is_origin_under_domain(origin: str, machine_domain: str) -> bool:
    """Whether ``origin`` is a bare https origin (no path/query) exactly one label under ``machine_domain``."""
    parsed = urlparse(origin)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    suffix = "." + machine_domain.lower()
    if not host.endswith(suffix):
        return False
    # Exactly one non-empty label (the dedicated auth label) -- not the bare
    # domain (which no longer routes), not a deeper host (which the relay
    # refuses to route and the wildcard cert does not cover), and not a
    # foreign host. Mirrors ``decide_frps_new_proxy``'s per-label model.
    label = host[: -len(suffix)]
    return bool(label) and "." not in label


def _authorize_self_query(machine_domain: str, next_url: str, callback_origin: str, state: str) -> str:
    """The query string that re-enters ``/share/authorize`` after the login page."""
    return urlencode(
        {
            "machine_domain": machine_domain,
            "next": next_url,
            "callback_origin": callback_origin,
            "state": state,
        }
    )


def _is_share_owner(user_id: str, share: dict[str, Any]) -> bool:
    """Whether ``user_id`` (a SuperTokens UUID) owns ``share``.

    A share row stores its owner as the user *label* (32-hex, hyphen-stripped)
    in the ``user_id`` column; the session's UUID is normalized the same way
    and compared. A malformed session id is simply not the owner.
    """
    try:
        session_label = shares_module.derive_share_user_label(user_id)
    except shares_module.InvalidShareCoordinateError:
        return False
    return session_label == share.get("user_id")


@router.get("/share/login")
def broker_login_redirect(request: Request) -> RedirectResponse:
    """Legacy broker login URL: permanently redirect to the merged accounts login page.

    Nothing links here anymore (the gateway redirects to ``/share/authorize``,
    which now bounces to ``/login`` itself), but any stale bookmark or cached
    redirect keeps working.
    """
    next_path = accounts_web_module.sanitize_local_next_path(request.query_params.get("next", "/"))
    suffix = f"?next={quote(next_path, safe='')}" if next_path != "/" else ""
    return RedirectResponse(url=f"/login{suffix}", status_code=308)


def _send_visitor_verification_email(user_id: str, email: str, continue_next_path: str) -> None:
    """Contextually send the verification email to an unverified share visitor.

    The share visit is the verification-gated action, so it is also the
    trigger for the send (server cooldown bounds repeats).
    ``continue_next_path`` is the local ``/share/authorize`` path the emailed
    link should route the visitor back through after verifying. Best-effort:
    the check-inbox page it accompanies already tells the visitor what to do.
    """
    try:
        recipe_user_id = auth_proxy_module.recipe_user_id_for_callers_email(user_id, email)
    except HTTPException as exc:
        logger.warning("Could not resolve login method for visitor verification email: %s", exc.detail)
        return
    try:
        auth_proxy_module.send_verification_email_with_cooldown(
            user_id=user_id,
            recipe_user_id=recipe_user_id,
            email=email,
            continue_next_path=continue_next_path,
        )
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        # A failed send must not fail the visit: the visitor still lands on
        # /check-inbox (which explains what to do), and the manage page offers
        # a resend. The cooldown slot was already released for the retry.
        emit_metric("verification_email_send_failed", 1, {"caller": "share_broker"})
        logger.warning("Could not send the visitor verification email", exc_info=exc)


@router.get("/share/authorize", response_model=None)
def broker_authorize(request: Request) -> RedirectResponse:
    """Authorize a visit to one shared workspace: require a confirmed session, then hand off a token.

    Redirect chain: gateway -> here (login/confirmation if needed) -> gateway's
    ``/_auth/callback`` with the minted JWT. ``state`` is the gateway's
    nonce, echoed both as a query param and inside the token so the callback
    can bind the response to its own pending request. Like the device
    handoff, the official flow routes an existing session through the login
    page's "Continue as ..." confirmation before ``confirmed=1`` comes back
    (an explicit sign-in confirms implicitly); ``confirmed`` is
    client-supplied, so that interstitial is a UX property -- the token only
    ever goes to a callback origin validated to be under this share's own
    domain.
    """
    with handle_endpoint_errors():
        machine_domain = request.query_params.get("machine_domain", "").lower()
        state = request.query_params.get("state", "")
        # ``next`` is the full origin URL the visitor was reaching, and
        # ``callback_origin`` is the workspace's dedicated auth origin (the one
        # label serving /_auth/*). Both are validated to be under this share's
        # domain before we redirect a signed token to either.
        next_url = request.query_params.get("next", "")
        callback_origin = request.query_params.get("callback_origin", "")
        if not machine_domain or not state:
            raise HTTPException(status_code=400, detail="machine_domain and state are required")
        if not _is_origin_under_domain(callback_origin, machine_domain):
            raise HTTPException(status_code=400, detail="callback_origin must be an origin under machine_domain")
        # ``next`` is optional; when present it must stay on this workspace, or
        # we drop it (the gateway falls back to a safe landing spot).
        safe_next = next_url if _is_url_under_domain(next_url, machine_domain) else ""
        identity = accounts_web_module.get_browser_session_identity(request)
        is_confirmed = request.query_params.get("confirmed") == "1"
        # A visitor with no session at all must sign in first, owner or not.
        if identity is None:
            login_next = f"/share/authorize?{_authorize_self_query(machine_domain, next_url, callback_origin, state)}"
            return RedirectResponse(url=f"/login?next={quote(login_next, safe='')}", status_code=302)
        user_id, session_email, is_email_verified = identity
        auth_module.stash_authenticated_user_for_access_log(request, user_id)
        share = shares_module.get_share_store().find_active_share_by_workspace_domain(machine_domain)
        if share is None:
            raise HTTPException(status_code=404, detail="No active share for this domain")
        # The owner fast path: the owner reaches their own workspace with no
        # "Continue as ..." interstitial and no verified-email requirement (the
        # gateway honors the ``owner`` claim regardless of grants). Everyone
        # else goes through the interstitial and the verified-email gate below.
        is_owner = _is_share_owner(user_id, share)
        if not is_owner and not is_confirmed:
            login_next = f"/share/authorize?{_authorize_self_query(machine_domain, next_url, callback_origin, state)}"
            return RedirectResponse(url=f"/login?next={quote(login_next, safe='')}", status_code=302)
        # The visitor's email IS the authorization identity for a non-owner
        # share grant, so this is one of the two places verification is required.
        if not is_owner and not is_email_verified:
            # The path that re-enters this authorization once the email is
            # verified: carried into the verification email's link AND to the
            # check-inbox page, so neither tab is a dead end. confirmed=1
            # because the visitor already passed the "Continue as ..."
            # interstitial to get here (and confirmed is a UX property only;
            # the token still goes solely to a validated same-share callback).
            authorize_next_path = (
                f"/share/authorize?{_authorize_self_query(machine_domain, next_url, callback_origin, state)}"
                "&confirmed=1"
            )
            _send_visitor_verification_email(user_id, session_email, authorize_next_path)
            # The check-your-inbox page lives in the hosted accounts bundle.
            return RedirectResponse(url=f"/check-inbox?next={quote(authorize_next_path, safe='')}", status_code=303)
        # The visit is authorized past this point: record it as an append-only
        # JSON line (visitor identity comes from the browser session, so this
        # is the one place visit history with identity exists).
        _log_share_visit_authorized(
            visitor_user_id=user_id,
            host_id=str(share.get("host_id", "")),
            owner_share_label=str(share.get("user_id", "")),
            workspace_domain=machine_domain,
            is_owner=is_owner,
        )
        handoff_token = mint_share_handoff_token(
            signing_key=_broker_signing_key(),
            user_id=user_id,
            email=session_email,
            machine_domain=machine_domain,
            nonce=state,
            is_owner=is_owner,
        )
        callback_query = urlencode({"token": handoff_token, "state": state, "next": safe_next})
        return RedirectResponse(url=f"{callback_origin}/_auth/callback?{callback_query}", status_code=302)


@router.get("/share/jwks.json")
def broker_jwks() -> JSONResponse:
    """The broker's public signing keys; workspace gateways verify handoff tokens against this."""
    with handle_endpoint_errors():
        jwks = build_broker_jwks(_broker_signing_key().public_key())
        return JSONResponse(content=jwks, headers={"Cache-Control": "public, max-age=300"})
