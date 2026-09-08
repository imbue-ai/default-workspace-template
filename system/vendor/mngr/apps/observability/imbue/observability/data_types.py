from functools import cached_property

from pydantic import AnyHttpUrl
from pydantic import Field
from pydantic import SecretStr
from pydantic import computed_field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.observability.primitives import BUGSINK_EVENT_RETENTION_DAYS
from imbue.observability.primitives import CollectorRole
from imbue.observability.primitives import ErrorsHostname
from imbue.observability.primitives import METRICS_RETENTION_DAYS
from imbue.observability.primitives import ObservabilityTierName
from imbue.observability.primitives import TelemetryHostname


class DashboardSummary(FrozenModel):
    """One dashboard's identity as the OpenObserve listing reports it."""

    dashboard_id: str = Field(description="Server-assigned dashboard id")
    title: str = Field(description="Dashboard title (the import flow's match key)")


class ObservabilityInstanceConfig(FrozenModel):
    """Everything needed to render one tier's OpenObserve instance host config.

    The instance is a single-writer OpenObserve process on a small VPS: parquet
    data lives in the tier's R2 bucket and metadata in the tier's Neon Postgres,
    so the host itself is disposable (see specs/minds-openobserve-telemetry.md).
    """

    tier: ObservabilityTierName = Field(description="Tier this instance serves (production / staging / dev)")
    telemetry_hostname: TelemetryHostname = Field(
        description="Public ingest hostname, Cloudflare-proxied (e.g. telemetry.minds-dev.com)"
    )
    root_user_email: str = Field(description="OpenObserve root (break-glass) account email, from Vault")
    root_user_password: SecretStr = Field(description="OpenObserve root account password, from Vault")
    meta_postgres_dsn: SecretStr = Field(
        description="Postgres DSN for OpenObserve's metadata store (direct, non -pooler Neon host)"
    )
    r2_endpoint_url: AnyHttpUrl = Field(
        description="S3 endpoint of the tier's R2 account (https://<account-id>.r2.cloudflarestorage.com)"
    )
    r2_bucket_name: str = Field(description="Per-tier R2 bucket holding the parquet stream data")
    r2_access_key_id: str = Field(description="Access key id of the bucket-scoped R2 token")
    r2_secret_access_key: SecretStr = Field(description="Secret access key of the bucket-scoped R2 token")
    origin_tls_certificate_pem: str = Field(
        description="Cloudflare origin certificate (PEM) caddy terminates TLS with; public material"
    )
    origin_tls_private_key_pem: SecretStr = Field(description="Private key (PEM) of the origin certificate")
    metrics_retention_days: int = Field(
        default=METRICS_RETENTION_DAYS,
        description="Instance-wide retention default; effectively the metrics retention (see primitives)",
    )


class BugsinkInstanceConfig(FrozenModel):
    """Everything needed to render one tier's Bugsink error-tracker instance host config.

    The instance is a single-writer Django monolith (gunicorn, one worker) on a
    small VPS behind the same split-plane caddy gate as the OpenObserve
    instances: only the Sentry-protocol ingest routes are public, the UI is
    SSH-tunnel-only, and all state lives in the tier's Neon Postgres so the
    host itself is disposable (see specs/minds-bugsink-error-tracking.md).
    """

    tier: ObservabilityTierName = Field(description="Tier this instance serves (production / staging / dev)")
    errors_hostname: ErrorsHostname = Field(
        description="Public ingest hostname, Cloudflare-proxied (e.g. errors.minds-dev.com)"
    )
    secret_key: SecretStr = Field(description="Django SECRET_KEY, from Vault")
    database_url: SecretStr = Field(
        description="Postgres DSN for the instance's database (direct, non -pooler Neon host)"
    )
    create_superuser: SecretStr = Field(
        description="Break-glass admin account as email:password; consumed on first boot only"
    )
    origin_tls_certificate_pem: str = Field(
        description="Cloudflare origin certificate (PEM) caddy terminates TLS with; public material"
    )
    origin_tls_private_key_pem: SecretStr = Field(description="Private key (PEM) of the origin certificate")
    max_event_age_days: int = Field(
        default=BUGSINK_EVENT_RETENTION_DAYS,
        description="Instance-wide event retention, enforced at digest time (see primitives)",
    )

    @computed_field
    @cached_property
    def base_url(self) -> str:
        # Django builds the project DSNs from BASE_URL, so it must carry the
        # public ingest hostname (never the loopback the UI is tunneled to).
        return f"https://{self.errors_hostname}"


class CollectorInstallConfig(FrozenModel):
    """Everything needed to render one machine's OpenTelemetry Collector install.

    One collector runs per bare-metal box, share-relay VPS, and observability
    instance host; it pushes host metrics and the systemd journal to the tier's
    ingest hostname over OTLP HTTP.
    """

    role: CollectorRole = Field(description="Machine class this collector runs on (picks scrapers + log stream)")
    tier: ObservabilityTierName = Field(description="Tier whose instance this collector reports to")
    ingest_url: AnyHttpUrl = Field(
        description="Base ingest URL (https://telemetry.<domain>, or the loopback OpenObserve URL on the instance itself)"
    )
    ingest_authorization_header_value: SecretStr = Field(
        description="Complete Authorization header value for ingestion (e.g. 'Basic <base64(email:password)>')"
    )


class SenderCredential(FrozenModel):
    """One sender class's minted ingest credential, destined for the tier's Vault entry."""

    sender_email: str = Field(description="The OpenObserve user this credential authenticates as")
    authorization_header_value: SecretStr = Field(
        description="Complete Authorization header value senders present (Basic <base64(email:password)>)"
    )
    is_newly_minted: bool = Field(
        description="Whether this pass created the user (False: the existing Vault credential was kept)"
    )
