import pytest
from pydantic import SecretStr

from imbue.minds_admin.envs.migrations import MigrationRunnerError
from imbue.minds_admin.envs.migrations import _direct_migration_dsn
from imbue.minds_admin.envs.migrations import parse_postgres_major_from_version_num


def test_direct_migration_dsn_strips_pooler_suffix_and_preserves_everything_else() -> None:
    pooled_dsn = SecretStr(
        "postgresql://neondb_owner:secret-word@ep-late-waterfall-ak6q71qd-pooler"
        ".c-3.us-west-2.aws.neon.tech/host_pool?sslmode=require"
    )

    direct_dsn = _direct_migration_dsn(pooled_dsn)

    assert direct_dsn.get_secret_value() == (
        "postgresql://neondb_owner:secret-word@ep-late-waterfall-ak6q71qd"
        ".c-3.us-west-2.aws.neon.tech/host_pool?sslmode=require"
    )


def test_direct_migration_dsn_preserves_explicit_port() -> None:
    pooled_dsn = SecretStr("postgresql://user@ep-abc-pooler.us-west-2.aws.neon.tech:5432/host_pool")

    direct_dsn = _direct_migration_dsn(pooled_dsn)

    assert direct_dsn.get_secret_value() == "postgresql://user@ep-abc.us-west-2.aws.neon.tech:5432/host_pool"


def test_direct_migration_dsn_leaves_already_direct_dsn_unchanged() -> None:
    direct_input = SecretStr(
        "postgresql://user:pw@ep-late-waterfall-ak6q71qd.c-3.us-west-2.aws.neon.tech/host_pool?sslmode=require"
    )

    assert _direct_migration_dsn(direct_input).get_secret_value() == direct_input.get_secret_value()


def test_direct_migration_dsn_leaves_non_neon_dsn_unchanged() -> None:
    local_input = SecretStr("postgresql://postgres:postgres@localhost:5432/host_pool")

    assert _direct_migration_dsn(local_input).get_secret_value() == local_input.get_secret_value()


def test_direct_migration_dsn_ignores_pooler_string_in_password() -> None:
    tricky_input = SecretStr("postgresql://user:pw-pooler.x@ep-abc.us-west-2.aws.neon.tech/host_pool")

    assert _direct_migration_dsn(tricky_input).get_secret_value() == tricky_input.get_secret_value()


def test_parse_postgres_major_from_version_num_reads_major_versions() -> None:
    assert parse_postgres_major_from_version_num("180006") == 18
    assert parse_postgres_major_from_version_num("170004\n") == 17


def test_parse_postgres_major_from_version_num_rejects_junk() -> None:
    with pytest.raises(MigrationRunnerError, match="server_version_num"):
        parse_postgres_major_from_version_num("PostgreSQL 18.6")
    with pytest.raises(MigrationRunnerError, match="server_version_num"):
        parse_postgres_major_from_version_num("")
