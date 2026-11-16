"""The relay (TURN) credential the gateway brokers for the Screen (specs/turn-relay/spec.md section 7).

Fetched from the connector's ``GET /turn/ice-servers`` with the share's relay token at
stack start and again at half life (the credential lives one hour), held in memory only
and served to the Screen service over the loopback ``GET /connect-info``. A refusal
(plan, quota, rate limit) or an unreachable connector leaves an empty list and the share
up: relay is an enhancement of the stream, never a precondition of sharing. A credential
already held keeps being served until it expires when a refresh is refused.
"""

from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import httpx
from share_grants.json_values import is_json_object

from share_gateway.log import log as _log

_FETCH_TIMEOUT_SECONDS = 15.0
# How long a refusal or an unreachable connector is believed before the next attempt.
REFUSAL_RETRY_INTERVAL = timedelta(minutes=5)
# The TTL the connector mints with; the refresh point when an answer carries no usable expiry.
DEFAULT_CREDENTIAL_LIFETIME = timedelta(hours=1)


class TurnCredential:
    """One answer of ``GET /turn/ice-servers``: the ICE servers as given and when they stop working."""

    def __init__(self, ice_servers: list[object], fetched_at: datetime, expires_at: datetime) -> None:
        self.ice_servers = ice_servers
        self.fetched_at = fetched_at
        self.expires_at = expires_at

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TurnCredential):
            return NotImplemented
        return vars(self) == vars(other)

    @property
    def refresh_at(self) -> datetime:
        return self.fetched_at + (self.expires_at - self.fetched_at) / 2


def _parse_expiry(raw: object, fetched_at: datetime) -> datetime:
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return fetched_at + DEFAULT_CREDENTIAL_LIFETIME


def parse_ice_servers_answer(body: object, fetched_at: datetime) -> TurnCredential | None:
    """The credential a 200 answer carries; None when the body is not the ``{"ice_servers", "expires_at"}`` shape."""
    if not is_json_object(body):
        return None
    ice_servers = body.get("ice_servers")
    if not isinstance(ice_servers, list):
        return None
    return TurnCredential(
        ice_servers=[server for server in ice_servers if is_json_object(server)],
        fetched_at=fetched_at,
        expires_at=_parse_expiry(body.get("expires_at"), fetched_at),
    )


def fetch_turn_credential(connector_url: str, relay_token: str, now: datetime) -> TurnCredential | None:
    """One fetch from the connector; None (logged) on a refusal, a transport failure or an unreadable answer."""
    try:
        response = httpx.get(
            f"{connector_url}/turn/ice-servers",
            headers={"Authorization": f"Bearer {relay_token}"},
            timeout=_FETCH_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        _log(f"TURN credential fetch failed: {exc}")
        return None
    if response.status_code != 200:
        _log(f"TURN credential refused ({response.status_code}): {response.text[:200]}")
        return None
    try:
        body = response.json()
    except ValueError:
        _log("TURN credential fetch returned non-JSON")
        return None
    credential = parse_ice_servers_answer(body, now)
    if credential is None:
        _log(f"TURN credential fetch returned an unreadable answer: {str(body)[:200]}")
    return credential


class TurnCredentialCache:
    """The one credential the gateway holds for a share, refreshed at half life and never written to disk."""

    def __init__(
        self,
        connector_url: str,
        relay_token: str,
        fetch: Callable[[str, str, datetime], TurnCredential | None] = fetch_turn_credential,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._connector_url = connector_url
        self._relay_token = relay_token
        self._fetch = fetch
        self._now = now
        self.credential: TurnCredential | None = None
        self.next_attempt_at: datetime | None = None

    def refresh_if_due(self) -> bool:
        """Fetch when there is no credential (and the last refusal has aged) or the held one reached half life;
        True when a fetch was attempted."""
        now = self._now()
        is_past_retry_wait = self.next_attempt_at is None or now >= self.next_attempt_at
        if self.credential is None:
            is_due = is_past_retry_wait
        else:
            is_due = now >= self.credential.refresh_at and is_past_retry_wait
        if not is_due:
            return False
        fetched = self._fetch(self._connector_url, self._relay_token, now)
        if fetched is not None:
            self.credential = fetched
            self.next_attempt_at = None
            _log(f"TURN credential fetched; it expires at {fetched.expires_at.isoformat()}")
        else:
            self.next_attempt_at = now + REFUSAL_RETRY_INTERVAL
            if self.credential is not None and now >= self.credential.expires_at:
                self.credential = None
        return True

    def ice_servers(self) -> list[object]:
        """What the Screen service is told: the held credential's servers while it is valid, else none."""
        # One read: the runner's tick thread may drop the credential between two.
        credential = self.credential
        if credential is None or self._now() >= credential.expires_at:
            return []
        return list(credential.ice_servers)
