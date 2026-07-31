from types import ModuleType


def test_direct_database_url_strips_pooler_suffix_and_preserves_everything_else(app_module: ModuleType) -> None:
    pooled_url = (
        "postgresql://neondb_owner:secret-word@ep-late-waterfall-ak6q71qd-pooler"
        ".c-3.us-west-2.aws.neon.tech/litellm_cost?sslmode=require"
    )

    direct_url = app_module._direct_database_url(pooled_url)

    assert direct_url == (
        "postgresql://neondb_owner:secret-word@ep-late-waterfall-ak6q71qd"
        ".c-3.us-west-2.aws.neon.tech/litellm_cost?sslmode=require"
    )


def test_direct_database_url_preserves_explicit_port(app_module: ModuleType) -> None:
    direct_url = app_module._direct_database_url("postgresql://user@ep-abc-pooler.us-west-2.aws.neon.tech:5432/db")

    assert direct_url == "postgresql://user@ep-abc.us-west-2.aws.neon.tech:5432/db"


def test_direct_database_url_leaves_already_direct_url_unchanged(app_module: ModuleType) -> None:
    direct_input = "postgresql://user:pw@ep-late-waterfall-ak6q71qd.c-3.us-west-2.aws.neon.tech/db?sslmode=require"

    assert app_module._direct_database_url(direct_input) == direct_input


def test_direct_database_url_leaves_non_neon_url_unchanged(app_module: ModuleType) -> None:
    local_input = "postgresql://postgres:postgres@localhost:5432/litellm"

    assert app_module._direct_database_url(local_input) == local_input


def test_direct_database_url_ignores_pooler_string_in_password(app_module: ModuleType) -> None:
    tricky_input = "postgresql://user:pw-pooler.x@ep-abc.us-west-2.aws.neon.tech/db"

    assert app_module._direct_database_url(tricky_input) == tricky_input


def test_is_connection_failure_output_matches_only_connection_error_codes(app_module: ModuleType) -> None:
    assert app_module._is_connection_failure_output(
        "Error: P1001: Can't reach database server at `ep-abc-pooler.neon.tech:5432`"
    )
    assert app_module._is_connection_failure_output("Error: P1002: The database server was reached but timed out.")
    assert app_module._is_connection_failure_output("Error: P1017: Server has closed the connection.")
    assert not app_module._is_connection_failure_output("Error: P3018: A migration failed to apply.")
    assert not app_module._is_connection_failure_output("The database schema is not in sync with your Prisma schema.")
