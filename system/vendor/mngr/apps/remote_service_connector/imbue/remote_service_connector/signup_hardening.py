"""IP-based hardening for the hosted signup surface (issue mngr-internal#467).

Layers, applied to both account-creation paths in ``accounts_web`` (the
password form and the Google OAuth callback's new-account branch):

- **Velocity limits**: per-IP (last hour) and per-subnet (/24 v4, /48 v6,
  last day) caps on signup attempts, counted from the Neon-backed
  ``signup_attempts`` table (the connector's containers share no memory).
  The caps are far above any human's rate and far below a flood's -- the
  2026-08 incident ran ~1,700 signups/hour from one IP.
- **IP reputation**: the IPinfo Max lookup API (``IPINFO_TOKEN``; residential
  proxies fold into the same ``proxy`` flag as open proxies), cached per IP
  in Neon so repeat lookups are free, plus a zero-dependency Tor-exit-list
  check that works with no token configured.
- **Graduated verdict**: tor/hosting IPs are blocked outright; vpn/proxy/
  relay IPs are stepped up to OAuth-only (Google survives -- reaching a real
  OAuth token is far more expensive for a bot than spinning email/password
  accounts); clean IPs are untouched.

Every gated attempt is recorded (verdict + outcome + IP + subnet), allowed
ones included, so the next flood is visible in real time instead of being
reconstructed from Modal logs after the fact.

Everything here fails OPEN, without exception: a Neon or provider outage
degrades signup to "Turnstile only" with a warning log. Turnstile (in
``accounts_web``) remains the sole fail-closed gate. Enforcement applies on
the tiers whose signup is restricted to the hosted surface (production /
staging, the ``is_json_signup_disabled`` line); dev/CI tiers record verdicts
but never refuse, matching their open headless-signup posture.

The trusted client IP is the ASGI socket peer -- see
``modal_app_kit.request_logging.client_ip_from_asgi_scope`` for the verified
Modal ingress semantics (X-Forwarded-For is stripped; other forwarding
headers are unsanitized and must never be consulted).
"""

import ipaddress
import logging
import os
import time
from enum import Enum
from typing import Final
from typing import Protocol

import httpx
import psycopg2
from fastapi import Request
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from imbue.modal_app_kit.request_logging import client_ip_from_asgi_scope
from imbue.remote_service_connector import db
from imbue.remote_service_connector.auth_proxy import is_json_signup_disabled

logger = logging.getLogger(__name__)

# Velocity caps. Generous for humans (an office NAT producing 15 signups in
# one hour would be extraordinary), ruinous for a single-IP flood. The subnet
# caps catch an attacker rotating within one hosting range. CGNAT / mobile
# carriers put many users behind one IP, which is why these are caps on
# *velocity* rather than "one account per IP".
MAX_SIGNUP_ATTEMPTS_PER_IP_PER_HOUR: Final[int] = 15
MAX_SIGNUP_ATTEMPTS_PER_SUBNET_PER_DAY: Final[int] = 50

# Aggregation prefixes for the per-subnet counters.
_IPV4_SUBNET_PREFIX_LENGTH: Final[int] = 24
_IPV6_SUBNET_PREFIX_LENGTH: Final[int] = 48

# Recorded attempts older than this are pruned opportunistically on insert;
# the rows exist for velocity counting and abuse investigation, not history.
_SIGNUP_ATTEMPT_RETENTION_DAYS: Final[int] = 90

# The recorded email is clamped to this many characters: attempts are
# recorded BEFORE any field validation runs (the gate is the first check on
# the signup form), so the value is raw request-body text and must be
# bounded before it reaches the table or the log. A legal email is at most
# 254 characters, so the clamp never touches a real address.
_MAX_RECORDED_EMAIL_CHARS: Final[int] = 320

# How long one IP's reputation lookup is reused before asking the provider
# again. Short enough to track anonymizer churn, long enough that a
# single-IP flood costs one upstream lookup per window.
IP_REPUTATION_CACHE_TTL_SECONDS: Final[int] = 6 * 60 * 60

# Ceiling on live provider lookups per rolling day (distinct IPs, counted
# from the cache's fetched_at stamps). A flood of unique IPs past this
# degrades to tor-list-only verdicts instead of running up the provider
# bill; the per-IP/subnet velocity caps have long since kicked in by then.
MAX_REPUTATION_LOOKUPS_PER_DAY: Final[int] = 5000

# Cached reputation rows older than this are pruned opportunistically on
# insert (the table would otherwise grow by one permanent row per distinct
# IP ever looked up). Must cover both the cache TTL and the budget window
# above, so pruning can never change a cache read or the budget count.
_IP_REPUTATION_CACHE_RETENTION_DAYS: Final[int] = 1

# Failures the Neon-backed stores fail open on: psycopg2.Error is any
# database failure, KeyError a missing DATABASE_URL (a connector deployed
# without the Neon secret must still sign users up -- the same rationale as
# attribution's fail-open catches).
_DB_FAIL_OPEN_ERRORS: Final = (KeyError, psycopg2.Error)

_IPINFO_TOKEN_ENV: Final[str] = "IPINFO_TOKEN"
_IPINFO_LOOKUP_URL_TEMPLATE: Final[str] = "https://api.ipinfo.io/lookup/{ip}"
_IPINFO_TIMEOUT_SECONDS: Final[float] = 5.0

# The Tor Project's bulk exit list: the free, no-token backstop that keeps
# Tor exits blocked even when no reputation provider is configured (dev
# tiers) or the provider is down. Refreshed at most hourly per container,
# with a backoff after a failed fetch so an outage cannot stall signups.
_TOR_EXIT_LIST_URL: Final[str] = "https://check.torproject.org/torbulkexitlist"
_TOR_EXIT_LIST_TTL_SECONDS: Final[float] = 60 * 60
_TOR_EXIT_LIST_RETRY_SECONDS: Final[float] = 300
_TOR_EXIT_LIST_TIMEOUT_SECONDS: Final[float] = 5.0


class SignupIpVerdict(str, Enum):
    """The reputation band a signup IP falls into (drives the graduated step-up)."""

    CLEAN = "clean"
    SUSPICIOUS = "suspicious"
    ABUSIVE = "abusive"


class SignupGateOutcome(str, Enum):
    """What the signup gate decided for one recorded attempt."""

    ALLOWED = "allowed"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    OAUTH_ONLY = "oauth_only"


class IpReputation(BaseModel):
    """One IP's anonymizer/hosting flags, as served by the reputation provider."""

    vpn: bool = Field(default=False)
    proxy: bool = Field(default=False)
    tor: bool = Field(default=False)
    relay: bool = Field(default=False)
    hosting: bool = Field(default=False)
    service: str = Field(default="", description="Provider-reported anonymizer service name, when known")


class SignupIpAssessment(BaseModel):
    """Everything the gate learned about one attempt's client IP."""

    client_ip: str | None = Field(description="The validated client IP, or None when unknown/unparseable")
    subnet: str | None = Field(description="The IP's aggregation subnet (/24 v4, /48 v6)")
    verdict: SignupIpVerdict = Field(description="The reputation band")
    reputation: IpReputation | None = Field(description="The provider/tor-list flags, or None when unavailable")
    is_rate_limited: bool = Field(description="Whether the velocity caps refuse this attempt")


def client_ip_for_request(request: Request) -> str | None:
    """The trusted, validated end-client IP of a request, or None.

    Socket peer only (see the module docstring). A peer that does not parse
    as an IP (test clients, unix sockets) yields None so callers degrade to
    "unknown IP" instead of feeding garbage into inet columns.
    """
    peer = client_ip_from_asgi_scope(request.scope)
    if peer == "-":
        return None
    try:
        ipaddress.ip_address(peer)
    except ValueError:
        logger.warning("Request peer %r is not a valid IP address; treating the client IP as unknown", peer)
        return None
    return peer


def subnet_for_client_ip(client_ip: str) -> str | None:
    """The aggregation subnet (/24 for IPv4, /48 for IPv6) an IP counts against."""
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        logger.warning("Cannot derive a subnet from invalid IP %r", client_ip)
        return None
    prefix_length = _IPV4_SUBNET_PREFIX_LENGTH if address.version == 4 else _IPV6_SUBNET_PREFIX_LENGTH
    return str(ipaddress.ip_network(f"{client_ip}/{prefix_length}", strict=False))


def classify_reputation(reputation: IpReputation) -> SignupIpVerdict:
    """Map reputation flags onto the graduated bands.

    tor/hosting are almost never a legitimate human's signup origin, so they
    block outright. vpn/proxy/relay only step up to OAuth-only: on the IPinfo
    Max plan the ``proxy`` flag deliberately includes residential proxies,
    whose false positives land on real households -- they must stay in the
    step-up band, never the block band.
    """
    if reputation.tor or reputation.hosting:
        return SignupIpVerdict.ABUSIVE
    elif reputation.vpn or reputation.proxy or reputation.relay:
        return SignupIpVerdict.SUSPICIOUS
    else:
        return SignupIpVerdict.CLEAN


def is_signup_ip_enforcement_enabled() -> bool:
    """Whether the gate refuses (vs. only records) on this tier.

    Rides the same tier line as the JSON-signup restriction: on dev/CI tiers
    the headless ``POST /auth/signup`` is open anyway, so refusing browser
    signups there would protect nothing while breaking the deployment tests'
    real-signup flows.
    """
    return is_json_signup_disabled()


# ---------------------------------------------------------------------------
# Stores (Neon-backed; in-memory fakes live in testing.py)
# ---------------------------------------------------------------------------


class SignupAttemptStore(Protocol):
    """Persistence for gated signup attempts (velocity counters + audit rows)."""

    def count_recent_attempts(self, client_ip: str, subnet: str | None) -> tuple[int, int]:
        """Return (attempts from this IP in the last hour, attempts from this subnet in the last day)."""
        ...

    def record_attempt(
        self,
        client_ip: str | None,
        subnet: str | None,
        email: str,
        signup_method: str,
        verdict: str,
        outcome: str,
        reputation_json: str | None,
    ) -> None: ...


class PostgresSignupAttemptStore:
    """Neon-backed attempt store; pruning is opportunistic on insert."""

    def count_recent_attempts(self, client_ip: str, subnet: str | None) -> tuple[int, int]:
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT"
                        " (SELECT COUNT(*) FROM signup_attempts"
                        "  WHERE client_ip = %s AND attempted_at > NOW() - INTERVAL '1 hour'),"
                        " (SELECT COUNT(*) FROM signup_attempts"
                        "  WHERE subnet = %s AND attempted_at > NOW() - INTERVAL '1 day')",
                        (client_ip, subnet),
                    )
                    row = cur.fetchone()
        return int(row[0]), int(row[1])

    def record_attempt(
        self,
        client_ip: str | None,
        subnet: str | None,
        email: str,
        signup_method: str,
        verdict: str,
        outcome: str,
        reputation_json: str | None,
    ) -> None:
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    # Opportunistic pruning keeps the table bounded without a cron.
                    cur.execute(
                        "DELETE FROM signup_attempts WHERE attempted_at < NOW() - make_interval(days => %s)",
                        (_SIGNUP_ATTEMPT_RETENTION_DAYS,),
                    )
                    cur.execute(
                        "INSERT INTO signup_attempts"
                        " (client_ip, subnet, email, signup_method, verdict, outcome, reputation)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (client_ip, subnet, email, signup_method, verdict, outcome, reputation_json),
                    )


class IpReputationCache(Protocol):
    """Cross-container cache of provider lookups, keyed by IP."""

    def get_fresh(self, client_ip: str, ttl_seconds: int) -> IpReputation | None:
        """Return the cached reputation when it is younger than the TTL, else None."""
        ...

    def store(self, client_ip: str, reputation: IpReputation) -> None: ...

    def count_lookups_in_last_day(self) -> int:
        """How many live provider lookups landed in the cache over the last day (the budget counter)."""
        ...


class PostgresIpReputationCache:
    """Neon-backed reputation cache; fetched_at doubles as the lookup-budget stamp."""

    def get_fresh(self, client_ip: str, ttl_seconds: int) -> IpReputation | None:
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT vpn, proxy, tor, relay, hosting, service FROM ip_reputation_cache"
                        " WHERE ip = %s AND fetched_at > NOW() - make_interval(secs => %s)",
                        (client_ip, ttl_seconds),
                    )
                    row = cur.fetchone()
        if row is None:
            return None
        return IpReputation(
            vpn=bool(row[0]),
            proxy=bool(row[1]),
            tor=bool(row[2]),
            relay=bool(row[3]),
            hosting=bool(row[4]),
            service=str(row[5] or ""),
        )

    def store(self, client_ip: str, reputation: IpReputation) -> None:
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    # Opportunistic pruning keeps the table bounded without a
                    # cron; rows past the retention are dead weight (older
                    # than both the cache TTL and the budget window).
                    cur.execute(
                        "DELETE FROM ip_reputation_cache WHERE fetched_at < NOW() - make_interval(days => %s)",
                        (_IP_REPUTATION_CACHE_RETENTION_DAYS,),
                    )
                    cur.execute(
                        "INSERT INTO ip_reputation_cache (ip, fetched_at, vpn, proxy, tor, relay, hosting, service)"
                        " VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (ip) DO UPDATE SET fetched_at = NOW(), vpn = EXCLUDED.vpn,"
                        " proxy = EXCLUDED.proxy, tor = EXCLUDED.tor, relay = EXCLUDED.relay,"
                        " hosting = EXCLUDED.hosting, service = EXCLUDED.service",
                        (
                            client_ip,
                            reputation.vpn,
                            reputation.proxy,
                            reputation.tor,
                            reputation.relay,
                            reputation.hosting,
                            reputation.service,
                        ),
                    )

    def count_lookups_in_last_day(self) -> int:
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM ip_reputation_cache WHERE fetched_at > NOW() - INTERVAL '1 day'")
                    row = cur.fetchone()
        return int(row[0])


# ---------------------------------------------------------------------------
# Reputation provider (IPinfo) and the Tor-exit-list backstop
# ---------------------------------------------------------------------------


class IpReputationProvider(Protocol):
    """One-method seam over the reputation service, so providers are swappable."""

    def fetch_reputation(self, client_ip: str) -> IpReputation | None:
        """Live lookup for one IP; None when no provider is configured on this tier."""
        ...


class IpinfoReputationProvider(BaseModel):
    """IPinfo Max lookup client (https://ipinfo.io/developers/max-api).

    The Max plan's self-service tokens serve ``https://api.ipinfo.io/lookup/{ip}``,
    NOT the enterprise ``/privacy`` module (a Max token gets 403 there --
    verified against a live Max token 2026-08-21). The lookup response nests
    the anonymizer flags in an ``anonymous`` object (``is_vpn``/``is_proxy``/
    ``is_tor``/``is_relay``/``is_res_proxy``) with ``is_hosting`` at the top
    level. Residential proxies (``is_res_proxy``) fold into our ``proxy``
    flag: their false positives land on real households, so they must stay in
    the step-up band, never the block band.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    transport: httpx.BaseTransport | None = Field(
        default=None, description="HTTP transport override (httpx.MockTransport in tests); None uses the real network"
    )

    def fetch_reputation(self, client_ip: str) -> IpReputation | None:
        token = os.environ.get(_IPINFO_TOKEN_ENV, "")
        if not token:
            return None
        # Bearer auth (not the ?token= query parameter) keeps the secret out
        # of URLs, which httpx echoes into its INFO logs and HTTPStatusError
        # messages (both of which land in Modal logs).
        with httpx.Client(transport=self.transport, timeout=_IPINFO_TIMEOUT_SECONDS) as client:
            response = client.get(
                _IPINFO_LOOKUP_URL_TEMPLATE.format(ip=client_ip),
                headers={"Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()
        payload = response.json()
        anonymous = payload.get("anonymous") or {}
        return IpReputation(
            vpn=bool(anonymous.get("is_vpn")),
            proxy=bool(anonymous.get("is_proxy")) or bool(anonymous.get("is_res_proxy")),
            tor=bool(anonymous.get("is_tor")),
            relay=bool(anonymous.get("is_relay")),
            hosting=bool(payload.get("is_hosting")),
            service=str(anonymous.get("name") or ""),
        )


class TorExitList(Protocol):
    """Membership check against the Tor Project's bulk exit list."""

    def is_tor_exit(self, client_ip: str) -> bool: ...


class CachedTorExitList(BaseModel):
    """Per-container hourly-refreshed copy of the Tor bulk exit list.

    Fails open: a fetch failure keeps serving the last good copy (or an empty
    set before the first success) and backs off before retrying, so a Tor
    Project outage can neither block signups nor hammer their endpoint.
    Deliberately lock-free: the worst concurrent-request race is a duplicate
    fetch of a small public file, and each field assignment is atomic.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    exit_ips: frozenset[str] = Field(default_factory=frozenset, description="The last successfully fetched exit set")
    next_fetch_monotonic: float = Field(default=0.0, description="time.monotonic() before which no fetch is attempted")
    transport: httpx.BaseTransport | None = Field(
        default=None, description="HTTP transport override (httpx.MockTransport in tests); None uses the real network"
    )

    def is_tor_exit(self, client_ip: str) -> bool:
        self._refresh_if_stale()
        return client_ip in self.exit_ips

    def _refresh_if_stale(self) -> None:
        if time.monotonic() < self.next_fetch_monotonic:
            return
        # Reserve the slot before fetching so concurrent requests mostly do
        # not all fetch; the failure path shortens it to the retry backoff.
        self.next_fetch_monotonic = time.monotonic() + _TOR_EXIT_LIST_TTL_SECONDS
        try:
            with httpx.Client(transport=self.transport, timeout=_TOR_EXIT_LIST_TIMEOUT_SECONDS) as client:
                response = client.get(_TOR_EXIT_LIST_URL)
            response.raise_for_status()
            fetched_ips = frozenset(line.strip() for line in response.text.splitlines() if line.strip())
        except httpx.HTTPError as exc:
            logger.warning("Could not refresh the Tor exit list (keeping the previous copy): %s", exc)
            self.next_fetch_monotonic = time.monotonic() + _TOR_EXIT_LIST_RETRY_SECONDS
            return
        self.exit_ips = fetched_ips
        logger.info("Refreshed the Tor exit list: %d exit IPs", len(fetched_ips))


# ---------------------------------------------------------------------------
# Module singletons (patched wholesale by the test fakes, like the
# device-code store in accounts_web)
# ---------------------------------------------------------------------------


_signup_attempt_store: SignupAttemptStore | None = None
_ip_reputation_cache: IpReputationCache | None = None
_ip_reputation_provider: IpReputationProvider | None = None
_tor_exit_list: TorExitList | None = None


def get_signup_attempt_store() -> SignupAttemptStore:
    global _signup_attempt_store
    if _signup_attempt_store is None:
        _signup_attempt_store = PostgresSignupAttemptStore()
    return _signup_attempt_store


def get_ip_reputation_cache() -> IpReputationCache:
    global _ip_reputation_cache
    if _ip_reputation_cache is None:
        _ip_reputation_cache = PostgresIpReputationCache()
    return _ip_reputation_cache


def get_ip_reputation_provider() -> IpReputationProvider:
    global _ip_reputation_provider
    if _ip_reputation_provider is None:
        _ip_reputation_provider = IpinfoReputationProvider()
    return _ip_reputation_provider


def get_tor_exit_list() -> TorExitList:
    global _tor_exit_list
    if _tor_exit_list is None:
        _tor_exit_list = CachedTorExitList()
    return _tor_exit_list


# ---------------------------------------------------------------------------
# Assessment and recording
# ---------------------------------------------------------------------------


def _resolve_reputation(client_ip: str, is_live_lookup_allowed: bool) -> IpReputation | None:
    """Cached-then-live reputation for one IP, unioned with the Tor exit list.

    Fails open at every step: a cache, budget, or provider failure logs a
    warning and degrades to whatever signal remains (worst case: Tor-list
    only). ``is_live_lookup_allowed`` is False for already-rate-limited
    attempts, so a single-source flood cannot burn provider lookups.
    """
    reputation: IpReputation | None = None
    cache = get_ip_reputation_cache()
    try:
        reputation = cache.get_fresh(client_ip, IP_REPUTATION_CACHE_TTL_SECONDS)
    except _DB_FAIL_OPEN_ERRORS as exc:
        logger.warning("IP reputation cache read failed (continuing without it): %s", exc)

    # A live provider lookup only when the cache missed, the attempt is not
    # already refused, and the daily budget has room.
    if reputation is None and is_live_lookup_allowed:
        is_budget_available = True
        try:
            is_budget_available = cache.count_lookups_in_last_day() < MAX_REPUTATION_LOOKUPS_PER_DAY
        except _DB_FAIL_OPEN_ERRORS as exc:
            logger.warning("IP reputation budget check failed (assuming budget available): %s", exc)
        if not is_budget_available:
            logger.warning(
                "IP reputation lookup budget exhausted (%d/day); degrading to tor-list-only verdicts",
                MAX_REPUTATION_LOOKUPS_PER_DAY,
            )
        else:
            try:
                reputation = get_ip_reputation_provider().fetch_reputation(client_ip)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("IP reputation lookup failed for %s (failing open): %s", client_ip, exc)
            if reputation is not None:
                try:
                    cache.store(client_ip, reputation)
                except _DB_FAIL_OPEN_ERRORS as exc:
                    logger.warning("IP reputation cache write failed: %s", exc)

    # The Tor list is a free union on top of whatever the provider said (or
    # the whole signal when no provider is configured).
    is_tor = False
    try:
        is_tor = get_tor_exit_list().is_tor_exit(client_ip)
    except httpx.HTTPError as exc:
        logger.warning("Tor exit list check failed (failing open): %s", exc)
    if is_tor:
        base = reputation if reputation is not None else IpReputation()
        reputation = IpReputation(
            vpn=base.vpn,
            proxy=base.proxy,
            tor=True,
            relay=base.relay,
            hosting=base.hosting,
            service=base.service,
        )
    return reputation


def assess_signup_ip(client_ip: str | None) -> SignupIpAssessment:
    """Assess one signup attempt's client IP: velocity counters + reputation verdict.

    Never raises: every backend failure degrades (with a warning) toward
    "clean and unlimited", keeping Turnstile the only fail-closed gate.
    """
    if client_ip is None:
        # No trustworthy IP (should not happen behind Modal's ingress): the
        # gate cannot key anything, so the attempt passes to the other gates.
        logger.warning("Signup attempt with no derivable client IP; skipping IP-based checks")
        return SignupIpAssessment(
            client_ip=None,
            subnet=None,
            verdict=SignupIpVerdict.CLEAN,
            reputation=None,
            is_rate_limited=False,
        )

    subnet = subnet_for_client_ip(client_ip)
    ip_hour_count = 0
    subnet_day_count = 0
    try:
        ip_hour_count, subnet_day_count = get_signup_attempt_store().count_recent_attempts(client_ip, subnet)
    except _DB_FAIL_OPEN_ERRORS as exc:
        logger.warning("Signup velocity counters unavailable (failing open): %s", exc)
    is_rate_limited = (
        ip_hour_count >= MAX_SIGNUP_ATTEMPTS_PER_IP_PER_HOUR
        or subnet_day_count >= MAX_SIGNUP_ATTEMPTS_PER_SUBNET_PER_DAY
    )

    reputation = _resolve_reputation(client_ip, is_live_lookup_allowed=not is_rate_limited)
    verdict = classify_reputation(reputation) if reputation is not None else SignupIpVerdict.CLEAN
    return SignupIpAssessment(
        client_ip=client_ip,
        subnet=subnet,
        verdict=verdict,
        reputation=reputation,
        is_rate_limited=is_rate_limited,
    )


def record_signup_attempt(
    assessment: SignupIpAssessment,
    email: str,
    signup_method: str,
    outcome: SignupGateOutcome,
) -> None:
    """Record one gated attempt (allowed ones included) and log its verdict.

    Fail-open: a failed write loses one audit row, never a signup.
    """
    reputation_json = assessment.reputation.model_dump_json() if assessment.reputation is not None else None
    recorded_email = email[:_MAX_RECORDED_EMAIL_CHARS]
    try:
        get_signup_attempt_store().record_attempt(
            client_ip=assessment.client_ip,
            subnet=assessment.subnet,
            email=recorded_email,
            signup_method=signup_method,
            verdict=assessment.verdict.value,
            outcome=outcome.value,
            reputation_json=reputation_json,
        )
    except _DB_FAIL_OPEN_ERRORS as exc:
        logger.warning("Could not record a signup attempt: %s", exc)
    # %r on the email: it arrives straight from the request body (unvalidated
    # at this point), and repr-escaping keeps a crafted value from forging
    # extra lines or fields in this abuse-visibility log.
    logger.info(
        "Signup gate: method=%s outcome=%s verdict=%s ip=%s subnet=%s email=%r",
        signup_method,
        outcome.value,
        assessment.verdict.value,
        assessment.client_ip,
        assessment.subnet,
        recorded_email,
    )
