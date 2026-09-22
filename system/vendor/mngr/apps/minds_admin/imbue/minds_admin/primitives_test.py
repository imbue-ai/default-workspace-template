from imbue.minds_admin.primitives import derive_user_id_prefix


def test_derive_user_id_prefix_matches_the_connector_key() -> None:
    # The connector strips the dashes and keeps the first 16 hex characters;
    # pool rows and bucket names are keyed by exactly that string.
    assert derive_user_id_prefix("4caec486-a38b-46f0-9d3e-1b2c3d4e5f60") == "4caec486a38b46f0"
