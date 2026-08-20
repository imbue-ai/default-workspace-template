"""Shared test factories for the observability instance tooling."""

from pydantic import AnyHttpUrl
from pydantic import SecretStr

from imbue.observability.data_types import BugsinkInstanceConfig
from imbue.observability.data_types import ObservabilityInstanceConfig
from imbue.observability.primitives import ErrorsHostname
from imbue.observability.primitives import ObservabilityTierName
from imbue.observability.primitives import TelemetryHostname


def make_instance_config(
    tier: str = "dev", root_user_password: str = "root-password-1"
) -> ObservabilityInstanceConfig:
    """One canned instance config; the R2 bucket name is derived from the tier."""
    return ObservabilityInstanceConfig(
        tier=ObservabilityTierName(tier),
        telemetry_hostname=TelemetryHostname("telemetry.minds-test.example"),
        root_user_email="root@example.com",
        root_user_password=SecretStr(root_user_password),
        meta_postgres_dsn=SecretStr("postgres://user:pw@db.example/openobserve"),
        r2_endpoint_url=AnyHttpUrl("https://account-1.r2.cloudflarestorage.com"),
        r2_bucket_name=f"minds-observability-{tier}",
        r2_access_key_id="r2-access-key-1",
        r2_secret_access_key=SecretStr("r2-secret-1"),
        origin_tls_certificate_pem="CERT-PEM",
        origin_tls_private_key_pem=SecretStr("KEY-PEM"),
    )


def make_bugsink_instance_config(tier: str = "dev", secret_key: str = "django-secret-key-1") -> BugsinkInstanceConfig:
    """One canned Bugsink instance config."""
    return BugsinkInstanceConfig(
        tier=ObservabilityTierName(tier),
        errors_hostname=ErrorsHostname("errors.minds-test.example"),
        secret_key=SecretStr(secret_key),
        database_url=SecretStr("postgres://user:pw@db.example/bugsink"),
        create_superuser=SecretStr("root@example.com:root-password-1"),
        origin_tls_certificate_pem="CERT-PEM",
        origin_tls_private_key_pem=SecretStr("KEY-PEM"),
    )
