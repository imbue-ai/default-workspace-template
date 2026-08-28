import pytest

from imbue.analytics.errors import AnalyticsConfigError
from imbue.analytics.settings import load_analytics_settings
from imbue.analytics.settings import load_collection_settings

_REQUIRED_ENV = {
    "ANALYTICS_METRICS_CATALOG_URL": "postgresql://metrics-user:pw@example.invalid/metrics",
    "ANALYTICS_TRANSCRIPTS_CATALOG_URL": "postgresql://transcripts-user:pw@example.invalid/transcripts",
    "ANALYTICS_OPS_DATABASE_URL": "postgresql://ops-user:pw@example.invalid/ops",
    "ANALYTICS_RSC_READONLY_DATABASE_URL": "postgresql://reader:pw@example.invalid/host_pool",
    "ANALYTICS_METRICS_R2_BUCKET": "analytics-metrics-testenv",
    "ANALYTICS_METRICS_R2_ACCESS_KEY_ID": "metrics-key-id",
    "ANALYTICS_METRICS_R2_SECRET_ACCESS_KEY": "metrics-secret",
    "ANALYTICS_TRANSCRIPTS_R2_BUCKET": "analytics-transcripts-testenv",
    "ANALYTICS_TRANSCRIPTS_R2_ACCESS_KEY_ID": "transcripts-key-id",
    "ANALYTICS_TRANSCRIPTS_R2_SECRET_ACCESS_KEY": "transcripts-secret",
    "ANALYTICS_LOGS_R2_BUCKET": "minds-observability-testenv",
    "ANALYTICS_LOGS_R2_ACCESS_KEY_ID": "logs-key-id",
    "ANALYTICS_LOGS_R2_SECRET_ACCESS_KEY": "logs-secret",
    "ANALYTICS_R2_ACCOUNT_ID": "cf-account-id",
}

_COLLECTION_TUNING_KEYS = (
    "ANALYTICS_COLLECTION_INTERVAL_SECONDS",
    "ANALYTICS_COLLECTION_PARALLELISM",
    "ANALYTICS_COLLECTION_WORKSPACE_TIMEOUT_SECONDS",
    "ANALYTICS_COLLECTION_RUN_BUDGET_BYTES",
)


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    # Clear the optional tuning knobs so the default-value assertions hold
    # regardless of the host environment.
    monkeypatch.delenv("ANALYTICS_LOGS_PARQUET_GLOB", raising=False)
    monkeypatch.delenv("ANALYTICS_LOGS_ENV_FILTER", raising=False)
    monkeypatch.delenv("ANALYTICS_AGGREGATION_WINDOW_DAYS", raising=False)
    for key in _COLLECTION_TUNING_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_load_analytics_settings_reads_all_required_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)

    settings = load_analytics_settings()

    assert settings.metrics_bucket == "analytics-metrics-testenv"
    assert settings.metrics_catalog_dsn.get_secret_value() == _REQUIRED_ENV["ANALYTICS_METRICS_CATALOG_URL"]
    assert settings.logs_r2_secret_access_key.get_secret_value() == "logs-secret"
    # Defaults apply when the optional tuning knobs are unset.
    assert settings.logs_parquet_glob == "files/default/logs/**/*.parquet"
    assert settings.logs_env_filter == ""
    assert settings.aggregation_window_days == 7


def test_load_analytics_settings_honors_optional_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ANALYTICS_LOGS_PARQUET_GLOB", "files/custom/**/*.parquet")
    monkeypatch.setenv("ANALYTICS_LOGS_ENV_FILTER", "dev-alice")
    monkeypatch.setenv("ANALYTICS_AGGREGATION_WINDOW_DAYS", "3")

    settings = load_analytics_settings()

    assert settings.logs_parquet_glob == "files/custom/**/*.parquet"
    assert settings.logs_env_filter == "dev-alice"
    assert settings.aggregation_window_days == 3


def test_load_analytics_settings_treats_an_empty_optional_value_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # The secret template documents an empty value as "declares the key but
    # leaves it unset", so it must behave exactly like an absent variable.
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ANALYTICS_AGGREGATION_WINDOW_DAYS", "")

    assert load_analytics_settings().aggregation_window_days == 7


def test_load_analytics_settings_raises_on_a_malformed_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ANALYTICS_AGGREGATION_WINDOW_DAYS", "seven")

    with pytest.raises(AnalyticsConfigError, match="ANALYTICS_AGGREGATION_WINDOW_DAYS"):
        load_analytics_settings()


def test_load_analytics_settings_raises_on_missing_required_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv("ANALYTICS_OPS_DATABASE_URL")

    with pytest.raises(AnalyticsConfigError, match="ANALYTICS_OPS_DATABASE_URL"):
        load_analytics_settings()


def test_settings_never_leak_secrets_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)

    settings = load_analytics_settings()

    assert "metrics-secret" not in repr(settings)
    assert "transcripts-secret" not in repr(settings)
    assert "pw@example.invalid" not in repr(settings)


def test_load_collection_settings_defaults_and_requires_the_pool_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("POOL_SSH_PRIVATE_KEY", "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n")

    settings = load_collection_settings()

    assert settings.interval_seconds == 3600
    assert settings.parallelism == 4
    assert settings.workspace_timeout_seconds == 600
    assert settings.run_budget_bytes == 256 * 1024 * 1024
    assert "fake" not in repr(settings)

    monkeypatch.delenv("POOL_SSH_PRIVATE_KEY")
    with pytest.raises(AnalyticsConfigError, match="POOL_SSH_PRIVATE_KEY"):
        load_collection_settings()


def test_load_collection_settings_honors_tuning_overrides_and_rejects_malformed_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("POOL_SSH_PRIVATE_KEY", "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n")
    monkeypatch.setenv("ANALYTICS_COLLECTION_INTERVAL_SECONDS", "120")
    monkeypatch.setenv("ANALYTICS_COLLECTION_PARALLELISM", "2")

    settings = load_collection_settings()
    assert settings.interval_seconds == 120
    assert settings.parallelism == 2

    monkeypatch.setenv("ANALYTICS_COLLECTION_PARALLELISM", "many")
    with pytest.raises(AnalyticsConfigError, match="ANALYTICS_COLLECTION_PARALLELISM"):
        load_collection_settings()

    # Zero would silently disable bounds (GNU timeout treats 0 as no timeout;
    # a zero pool has no workers), so non-positive overrides must be refused.
    monkeypatch.setenv("ANALYTICS_COLLECTION_PARALLELISM", "2")
    monkeypatch.setenv("ANALYTICS_COLLECTION_WORKSPACE_TIMEOUT_SECONDS", "0")
    with pytest.raises(AnalyticsConfigError, match="workspace_timeout_seconds"):
        load_collection_settings()
