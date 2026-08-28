import tomllib
from datetime import datetime
from datetime import timezone

import pytest
from inline_snapshot import snapshot
from pydantic import SecretStr

from imbue.minds_admin.envs.providers.analytics_analysts import AnalystCredentials
from imbue.minds_admin.envs.providers.analytics_analysts import AnalyticsLake
from imbue.minds_admin.envs.providers.analytics_analysts import LakeCredentials
from imbue.minds_admin.envs.providers.analytics_analysts import analyst_name_and_lake_from_token_name
from imbue.minds_admin.envs.providers.analytics_analysts import analyst_name_from_role_name
from imbue.minds_admin.envs.providers.analytics_analysts import analyst_role_name
from imbue.minds_admin.envs.providers.analytics_analysts import analyst_token_name
from imbue.minds_admin.envs.providers.analytics_analysts import render_analyst_credentials_toml
from imbue.minds_admin.primitives import AnalystName
from imbue.minds_admin.primitives import InvalidAnalystNameError


def _credentials(is_transcripts_included: bool, metrics_secret: str = "msecret") -> AnalystCredentials:
    transcripts = (
        LakeCredentials(
            catalog_dsn=SecretStr("postgresql://analyst_alice_w:tpw@ep-t.aws.neon.tech/transcripts?sslmode=require"),
            r2_bucket="analytics-transcripts-production",
            r2_access_key_id="tkid",
            r2_secret_access_key=SecretStr("tsecret"),
        )
        if is_transcripts_included
        else None
    )
    return AnalystCredentials(
        analyst_name=AnalystName("alice_w"),
        role_name="analyst_alice_w",
        r2_account_id="cfacct",
        metrics=LakeCredentials(
            catalog_dsn=SecretStr("postgresql://analyst_alice_w:mpw@ep-m.aws.neon.tech/metrics?sslmode=require"),
            r2_bucket="analytics-metrics-production",
            r2_access_key_id="mkid",
            r2_secret_access_key=SecretStr(metrics_secret),
        ),
        transcripts=transcripts,
    )


def test_analyst_names_reject_invalid_handles_and_normalize_whitespace() -> None:
    assert AnalystName("  alice_w  ") == "alice_w"

    with pytest.raises(InvalidAnalystNameError):
        AnalystName("Alice")
    with pytest.raises(InvalidAnalystNameError):
        AnalystName("a")
    with pytest.raises(InvalidAnalystNameError):
        AnalystName("1alice")
    with pytest.raises(InvalidAnalystNameError):
        AnalystName("alice-w")
    with pytest.raises(InvalidAnalystNameError):
        AnalystName("alice; DROP ROLE admin")


def test_role_and_token_names_are_deterministic_and_round_trip() -> None:
    name = AnalystName("alice_w")

    role_name = analyst_role_name(name)
    metrics_token = analyst_token_name(name, AnalyticsLake.METRICS)
    transcripts_token = analyst_token_name(name, AnalyticsLake.TRANSCRIPTS)

    assert role_name == "analyst_alice_w"
    assert metrics_token == "analytics-analyst-alice_w-metrics-ro"
    assert transcripts_token == "analytics-analyst-alice_w-transcripts-ro"
    assert analyst_name_from_role_name(role_name) == "alice_w"
    assert analyst_name_and_lake_from_token_name(metrics_token) == ("alice_w", AnalyticsLake.METRICS)
    assert analyst_name_and_lake_from_token_name(transcripts_token) == ("alice_w", AnalyticsLake.TRANSCRIPTS)


def test_non_analyst_role_and_token_names_parse_to_none() -> None:
    assert analyst_name_from_role_name("analytics_reader") is None
    assert analyst_name_from_role_name("analyst_") is None
    assert analyst_name_and_lake_from_token_name("analytics-metrics-production-rw") is None
    assert analyst_name_and_lake_from_token_name("analytics-analyst-metrics-ro") is None
    assert analyst_name_and_lake_from_token_name("analytics-analyst-bob-logs-ro") is None


def test_rendered_credentials_toml_documents_both_lakes_with_real_values() -> None:
    document = render_analyst_credentials_toml(
        _credentials(is_transcripts_included=True),
        "production",
        datetime(2026, 8, 27, 21, 30, tzinfo=timezone.utc),
    )

    assert document == snapshot("""\
# Imbue minds analytics (production) -- read-only analyst credentials for 'alice_w'.
# Minted 2026-08-27 21:30 UTC by `minds-admin analytics analyst add`; re-running that
# command rotates these credentials, and `... analyst remove` revokes them.
# SECRETS: deliver privately and store like a password.
#
# What this grants: read-only SQL over the minds analytics lakes, queried with
# DuckDB from your own machine (there is no dashboard service). Tables,
# worked-example queries, data start dates, and gotchas are documented in
# apps/analytics/reports/README.md in the mngr repo -- start there.
#
# Quick start (`uv run --with duckdb --with pytz python`, or the duckdb CLI):
#
#   INSTALL ducklake; LOAD ducklake;
#   INSTALL postgres; LOAD postgres;
#   INSTALL httpfs; LOAD httpfs;
#   CREATE SECRET metrics_bucket (
#       TYPE r2,
#       KEY_ID 'mkid',
#       SECRET 'msecret',
#       ACCOUNT_ID 'cfacct',
#       SCOPE 'r2://analytics-metrics-production'
#   );
#   ATTACH 'ducklake:postgres:postgresql://analyst_alice_w:mpw@ep-m.aws.neon.tech/metrics?sslmode=require' AS metrics (READ_ONLY);
#   CREATE SECRET transcripts_bucket (
#       TYPE r2,
#       KEY_ID 'tkid',
#       SECRET 'tsecret',
#       ACCOUNT_ID 'cfacct',
#       SCOPE 'r2://analytics-transcripts-production'
#   );
#   ATTACH 'ducklake:postgres:postgresql://analyst_alice_w:tpw@ep-t.aws.neon.tech/transcripts?sslmode=require' AS transcripts (READ_ONLY);
#   SELECT * FROM metrics.gold.activity LIMIT 10;

analyst = "alice_w"
environment = "production"
r2_account_id = "cfacct"

[metrics]
catalog_dsn = "postgresql://analyst_alice_w:mpw@ep-m.aws.neon.tech/metrics?sslmode=require"
r2_bucket = "analytics-metrics-production"
r2_access_key_id = "mkid"
r2_secret_access_key = "msecret"

[transcripts]
catalog_dsn = "postgresql://analyst_alice_w:tpw@ep-t.aws.neon.tech/transcripts?sslmode=require"
r2_bucket = "analytics-transcripts-production"
r2_access_key_id = "tkid"
r2_secret_access_key = "tsecret"
""")


def test_rendered_credentials_toml_stays_parseable_for_non_ascii_values() -> None:
    # An astral-plane character (Gothic hwair): json-style surrogate-pair
    # \uXXXX escapes for it would be rejected by TOML parsers, so the renderer
    # must emit it literally.
    tricky_secret = "s3cret-\U00010348-value"
    credentials = _credentials(is_transcripts_included=False, metrics_secret=tricky_secret)

    document = render_analyst_credentials_toml(credentials, "production", datetime(2026, 8, 27, tzinfo=timezone.utc))

    assert tomllib.loads(document)["metrics"]["r2_secret_access_key"] == tricky_secret


def test_rendered_credentials_toml_omits_transcripts_when_opted_out() -> None:
    document = render_analyst_credentials_toml(
        _credentials(is_transcripts_included=False),
        "production",
        datetime(2026, 8, 27, 21, 30, tzinfo=timezone.utc),
    )

    assert "[metrics]" in document
    assert "[transcripts]" not in document
    assert "transcripts_bucket" not in document
