import re
from enum import auto
from typing import Final
from typing import Self

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.imbue_common.primitives import NonEmptyStr

# A single DNS label: lowercase alphanumeric runs joined by single hyphens, no
# leading/trailing/consecutive hyphens, 1..63 chars.
_DNS_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"^(?=.{1,63}$)[a-z0-9]+(?:-[a-z0-9]+)*$")

# The tiers an observability instance can serve. There is exactly one instance
# per tier; every dev-* and ci-* env reports to the shared "dev" instance
# (their Modal apps live in the shared minds-dev workspace, whose
# workspace-level OpenTelemetry export is necessarily tier-wide).
_KNOWN_TIER_NAMES: Final[frozenset[str]] = frozenset({"production", "staging", "dev"})


class ObservabilityTierName(NonEmptyStr):
    """The tier one observability instance serves: ``production``, ``staging``, or ``dev``."""

    def __new__(cls, value: str) -> "ObservabilityTierName":
        instance = super().__new__(cls, value)
        if str(instance) not in _KNOWN_TIER_NAMES:
            raise InvalidPrimitiveValueError(
                f"{cls.__name__} must be one of {sorted(_KNOWN_TIER_NAMES)}; got {value!r} "
                "(dev-*/ci-* envs all report to the shared 'dev' instance)"
            )
        return instance


class PublicIngestHostname(NonEmptyStr):
    """A Cloudflare-proxied public ingest hostname of one tier's instance.

    Rendered into Caddyfiles, DNS upserts, and every sender's config, so it
    must be a real DNS name: dot-joined lowercase DNS labels.
    """

    def __new__(cls, value: str) -> Self:
        instance = super().__new__(cls, value)
        if any(_DNS_LABEL_RE.match(label) is None for label in str(instance).split(".")):
            raise InvalidPrimitiveValueError(
                f"{cls.__name__} must be dot-joined lowercase DNS labels "
                f"(alphanumeric runs joined by single hyphens); got {value!r}"
            )
        return instance


class TelemetryHostname(PublicIngestHostname):
    """The OpenObserve instance's public OTLP ingest hostname (e.g. ``telemetry.minds-dev.com``)."""


class ErrorsHostname(PublicIngestHostname):
    """The Bugsink instance's public Sentry-protocol ingest hostname (e.g. ``errors.minds-dev.com``)."""


class CollectorRole(UpperCaseStrEnum):
    """Which kind of machine an OpenTelemetry Collector runs on.

    The role picks the process scrapers (which processes are worth per-process
    metrics on that machine class) and the log stream the collector ships into.
    """

    BOX = auto()
    RELAY = auto()
    INSTANCE = auto()


class SenderClass(UpperCaseStrEnum):
    """One class of machine ingest credential, rotated independently of the others.

    MODAL is consumed by the Modal workspace-level OpenTelemetry integration
    (configured by hand in each workspace's settings); BOXES by the bare-metal
    slice boxes' collectors; RELAYS by the share-relay VPS collectors.
    """

    MODAL = auto()
    BOXES = auto()
    RELAYS = auto()


# The OpenObserve HTTP port; the caddy ingest gate reverse-proxies to it on
# loopback, and operators reach the UI at it through an SSH tunnel.
OPENOBSERVE_HTTP_PORT: Final[int] = 5080

# The Bugsink (gunicorn) HTTP port, same split-plane shape: caddy
# reverse-proxies only the Sentry-protocol ingest routes to it on loopback,
# and operators reach the Django UI at it through an SSH tunnel.
BUGSINK_HTTP_PORT: Final[int] = 8300

# One OpenObserve organization per instance -- the tier IS the isolation
# boundary, so the built-in default org suffices everywhere.
OPENOBSERVE_ORGANIZATION: Final[str] = "default"

# Retention (spec: minds-openobserve-telemetry.md). Metrics keep 25 months of
# capacity/seasonality history; log lines can carry user-identifying data so
# they keep 90 days. The metrics value is the instance-wide default
# (ZO_COMPACT_DATA_RETENTION_DAYS -- OpenObserve maps each OTLP metric to its
# own stream, so per-metric-stream overrides would not stick); the known log
# streams are overridden down per stream at provisioning time.
METRICS_RETENTION_DAYS: Final[int] = 760
LOGS_RETENTION_DAYS: Final[int] = 90

# Bugsink instance-wide event retention (MAX_EVENT_AGE_DAYS), enforced at
# digest time under eager mode. Short retention is the compensating control
# for prompt-bearing LiteLLM failure payloads; issue rows (titles, counts,
# first/last-seen) survive event deletion (spec: minds-bugsink-error-tracking.md).
BUGSINK_EVENT_RETENTION_DAYS: Final[int] = 30

# The log stream each sender class ships into (via the ``stream-name`` header
# OpenObserve reads on its OTLP HTTP logs endpoint). Modal's is set as an
# OTEL_HEADER_stream-name entry in the workspace integration secret; the
# collectors set theirs on the otlphttp exporter.
LOG_STREAM_NAME_BY_COLLECTOR_ROLE: Final[dict[CollectorRole, str]] = {
    CollectorRole.BOX: "box_logs",
    CollectorRole.RELAY: "relay_logs",
    CollectorRole.INSTANCE: "instance_logs",
}
MODAL_LOG_STREAM_NAME: Final[str] = "modal_logs"

# Every log stream whose retention is overridden down to LOGS_RETENTION_DAYS
# at provisioning time (streams only exist once data has arrived, so the
# override is retried on every provisioning pass).
ALL_LOG_STREAM_NAMES: Final[tuple[str, ...]] = (
    MODAL_LOG_STREAM_NAME,
    LOG_STREAM_NAME_BY_COLLECTOR_ROLE[CollectorRole.BOX],
    LOG_STREAM_NAME_BY_COLLECTOR_ROLE[CollectorRole.RELAY],
    LOG_STREAM_NAME_BY_COLLECTOR_ROLE[CollectorRole.INSTANCE],
)
