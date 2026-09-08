from pydantic import SecretStr

from imbue.minds.envs.primitives import DevEnvName
from imbue.minds_admin.envs.providers.analytics_stack import AnalyticsStackRecord
from imbue.minds_admin.envs.providers.analytics_stack import analytics_secret_values_from_record
from imbue.minds_admin.envs.providers.analytics_stack import analytics_token_names_for
from imbue.minds_admin.envs.providers.analytics_stack import build_reader_dsn
from imbue.minds_admin.envs.providers.analytics_stack import local_secret_values_from_record
from imbue.minds_admin.envs.providers.analytics_stack import metrics_bucket_name_for
from imbue.minds_admin.envs.providers.analytics_stack import record_from_local_secrets
from imbue.minds_admin.envs.providers.analytics_stack import transcripts_bucket_name_for


def _record() -> AnalyticsStackRecord:
    return AnalyticsStackRecord(
        metrics_catalog_dsn=SecretStr("postgresql://u:p@host/metrics"),
        transcripts_catalog_dsn=SecretStr("postgresql://u:p@host/transcripts"),
        ops_dsn=SecretStr("postgresql://u:p@host/ops"),
        rsc_readonly_dsn=SecretStr("postgresql://reader:pw@host/host_pool"),
        metrics_bucket="analytics-metrics-dev-x",
        metrics_access_key_id="mkid",
        metrics_secret_access_key=SecretStr("msecret"),
        transcripts_bucket="analytics-transcripts-dev-x",
        transcripts_access_key_id="tkid",
        transcripts_secret_access_key=SecretStr("tsecret"),
        logs_bucket="minds-observability-dev",
        logs_access_key_id="lkid",
        logs_secret_access_key=SecretStr("lsecret"),
        r2_account_id="cfacct",
    )


def test_local_secret_round_trip_reconstructs_the_record() -> None:
    record = _record()

    persisted = local_secret_values_from_record(record)
    rebuilt = record_from_local_secrets(persisted)

    assert rebuilt == record


def test_record_from_local_secrets_returns_none_when_any_key_is_missing_or_blank() -> None:
    persisted = local_secret_values_from_record(_record())

    missing = dict(persisted)
    del missing["ANALYTICS_OPS_DATABASE_URL"]
    assert record_from_local_secrets(missing) is None

    blank = dict(persisted)
    blank["ANALYTICS_METRICS_R2_BUCKET"] = SecretStr("")
    assert record_from_local_secrets(blank) is None

    assert record_from_local_secrets({}) is None


def test_analytics_secret_values_carry_the_env_filter_and_interval() -> None:
    values = analytics_secret_values_from_record(_record(), logs_env_filter="dev-x", collection_interval_seconds=120)

    assert values["ANALYTICS_LOGS_ENV_FILTER"] == "dev-x"
    assert values["ANALYTICS_COLLECTION_INTERVAL_SECONDS"] == "120"
    assert values["ANALYTICS_OPS_DATABASE_URL"] == "postgresql://u:p@host/ops"
    assert values["ANALYTICS_METRICS_R2_SECRET_ACCESS_KEY"] == "msecret"


def test_analytics_secret_values_omit_the_interval_when_not_overridden() -> None:
    values = analytics_secret_values_from_record(_record(), logs_env_filter="", collection_interval_seconds=None)

    assert "ANALYTICS_COLLECTION_INTERVAL_SECONDS" not in values
    assert values["ANALYTICS_LOGS_ENV_FILTER"] == ""


def test_build_reader_dsn_swaps_credentials_and_percent_encodes_the_password() -> None:
    admin = "postgresql://admin:adminpw@ep-x-pooler.c-4.aws.neon.tech/host_pool?sslmode=require"

    reader = build_reader_dsn(admin, role="analytics_reader", password="p@ss/word")

    assert reader == ("postgresql://analytics_reader:p%40ss%2Fword@ep-x.c-4.aws.neon.tech/host_pool?sslmode=require")


def test_stack_names_are_deterministic_per_env() -> None:
    name = DevEnvName("dev-alice")

    assert metrics_bucket_name_for(name) == "analytics-metrics-dev-alice"
    assert transcripts_bucket_name_for(name) == "analytics-transcripts-dev-alice"
    assert analytics_token_names_for(name) == (
        "analytics-metrics-dev-alice-rw",
        "analytics-transcripts-dev-alice-rw",
        "analytics-logs-dev-alice-ro",
    )
