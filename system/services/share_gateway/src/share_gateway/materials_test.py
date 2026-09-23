import re
from pathlib import Path

import pytest

from share_gateway.materials import discard_signing_secret
from share_gateway.materials import load_or_create_auth_label
from share_gateway.materials import load_or_create_signing_secret
from share_gateway.materials import parse_share_materials
from share_gateway.materials import read_share_materials

_VALID = """
export SHARE_WORKSPACE_DOMAIN=host-aaaa.bbbb.us1.imbueminds.com
export SHARE_RELAY_TOKEN="tok-123"
export SHARE_CONNECTOR_URL=https://connector.example.com/
export SHARE_BROKER_URL='https://accounts.example.com'
export SHARE_CHROME_ORIGIN=https://minds.imbue.com/
"""


def test_parse_share_materials_reads_all_fields() -> None:
    materials = parse_share_materials(_VALID)
    assert materials is not None
    assert materials.workspace_domain == "host-aaaa.bbbb.us1.imbueminds.com"
    assert materials.relay_token == "tok-123"
    assert materials.connector_url == "https://connector.example.com"
    assert materials.broker_url == "https://accounts.example.com"
    assert materials.chrome_origin == "https://minds.imbue.com"


def test_parse_share_materials_defaults_chrome_origin_to_empty_when_absent() -> None:
    without_chrome = "\n".join(line for line in _VALID.splitlines() if "SHARE_CHROME_ORIGIN" not in line)
    materials = parse_share_materials(without_chrome)
    assert materials is not None
    assert materials.chrome_origin == ""


def test_parse_share_materials_rejects_missing_or_malformed_keys() -> None:
    assert parse_share_materials("") is None
    assert parse_share_materials("export SHARE_WORKSPACE_DOMAIN=x") is None
    assert parse_share_materials(_VALID.replace("tok-123", "")) is None


def test_read_share_materials_handles_missing_file(tmp_path: Path) -> None:
    assert read_share_materials(tmp_path / "absent.env") is None
    materials_path = tmp_path / "share.env"
    materials_path.write_text(_VALID)
    materials = read_share_materials(materials_path)
    assert materials is not None
    assert materials.relay_token == "tok-123"


def test_signing_secret_is_created_once_and_reused_under_the_same_relay_token(tmp_path: Path) -> None:
    secret_path = tmp_path / "signing_key"
    first = load_or_create_signing_secret(secret_path, "tok-123")
    second = load_or_create_signing_secret(secret_path, "tok-123")
    assert first == second
    assert len(first) > 32
    assert (secret_path.stat().st_mode & 0o777) == 0o600
    # The file binds the secret to its share by a digest, never by the token itself.
    assert "tok-123" not in secret_path.read_text()


def test_signing_secret_minted_under_one_relay_token_is_replaced_under_another(tmp_path: Path) -> None:
    # An unshare and re-share the runner was down for leaves the earlier share's secret on disk with
    # materials present; the new share's relay token must not pick it up, or the old cookies would open it.
    secret_path = tmp_path / "signing_key"
    earlier_share = load_or_create_signing_secret(secret_path, "tok-123")

    later_share = load_or_create_signing_secret(secret_path, "tok-456")

    assert later_share != earlier_share
    assert load_or_create_signing_secret(secret_path, "tok-456") == later_share
    assert load_or_create_signing_secret(secret_path, "tok-123") != earlier_share


@pytest.mark.parametrize(
    "stored_text",
    [
        "a-bare-secret-from-an-earlier-gateway",
        '["a-bare-secret-from-an-earlier-gateway"]',
        '{"relay_token_sha256": "0123abcd"}',
        '{"secret": "", "relay_token_sha256": "0123abcd"}',
        '{"secret": 12345, "relay_token_sha256": "0123abcd"}',
        '{"secret": "a-bare-secret-from-an-earlier-gateway"}',
    ],
)
def test_signing_secret_file_without_a_relay_token_binding_is_replaced(tmp_path: Path, stored_text: str) -> None:
    secret_path = tmp_path / "signing_key"
    secret_path.write_text(stored_text)

    minted = load_or_create_signing_secret(secret_path, "tok-123")

    assert minted not in stored_text
    assert load_or_create_signing_secret(secret_path, "tok-123") == minted
    assert (secret_path.stat().st_mode & 0o777) == 0o600


def test_discarding_the_signing_secret_makes_the_next_share_mint_a_different_one(tmp_path: Path) -> None:
    # Unshare deletes the secret so every session it signed stops verifying;
    # the re-share must not resurrect it.
    secret_path = tmp_path / "signing_key"
    before_unshare = load_or_create_signing_secret(secret_path, "tok-123")

    assert discard_signing_secret(secret_path) is True

    assert not secret_path.exists()
    assert load_or_create_signing_secret(secret_path, "tok-123") != before_unshare
    # Discarding when nothing was ever minted is a no-op, not an error, and says so (the runner
    # logs a removal only when there was one).
    assert discard_signing_secret(tmp_path / "never-minted") is False


def test_auth_label_is_created_once_reused_and_well_formed(tmp_path: Path) -> None:
    label_path = tmp_path / "share_auth_label"
    first = load_or_create_auth_label(label_path)
    second = load_or_create_auth_label(label_path)
    assert first == second
    assert re.match(r"^auth-[a-z0-9]{8}$", first)
    assert (label_path.stat().st_mode & 0o777) == 0o600


def test_auth_label_replaces_a_malformed_stored_value(tmp_path: Path) -> None:
    label_path = tmp_path / "share_auth_label"
    label_path.write_text("not-a-valid-auth-label")
    regenerated = load_or_create_auth_label(label_path)
    assert re.match(r"^auth-[a-z0-9]{8}$", regenerated)
