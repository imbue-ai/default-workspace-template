from imbue.modal_app_kit.database import direct_database_url


def test_direct_database_url_strips_pooler_suffix_and_preserves_everything_else() -> None:
    pooled_url = (
        "postgresql://neondb_owner:secret-word@ep-late-waterfall-ak6q71qd-pooler"
        ".c-3.us-west-2.aws.neon.tech/litellm_cost?sslmode=require"
    )

    direct_url = direct_database_url(pooled_url)

    assert direct_url == (
        "postgresql://neondb_owner:secret-word@ep-late-waterfall-ak6q71qd"
        ".c-3.us-west-2.aws.neon.tech/litellm_cost?sslmode=require"
    )


def test_direct_database_url_preserves_explicit_port() -> None:
    direct_url = direct_database_url("postgresql://user@ep-abc-pooler.us-west-2.aws.neon.tech:5432/db")

    assert direct_url == "postgresql://user@ep-abc.us-west-2.aws.neon.tech:5432/db"


def test_direct_database_url_leaves_already_direct_url_unchanged() -> None:
    direct_input = "postgresql://user:pw@ep-late-waterfall-ak6q71qd.c-3.us-west-2.aws.neon.tech/db?sslmode=require"

    assert direct_database_url(direct_input) == direct_input


def test_direct_database_url_leaves_non_neon_url_unchanged() -> None:
    local_input = "postgresql://postgres:postgres@localhost:5432/litellm"

    assert direct_database_url(local_input) == local_input


def test_direct_database_url_ignores_pooler_string_in_password() -> None:
    tricky_input = "postgresql://user:pw-pooler.x@ep-abc.us-west-2.aws.neon.tech/db"

    assert direct_database_url(tricky_input) == tricky_input
