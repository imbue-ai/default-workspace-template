import re
from pathlib import Path

from share_gateway.materials import load_or_create_auth_label
from share_gateway.materials import load_or_create_signing_secret
from share_gateway.materials import parse_share_materials
from share_gateway.materials import read_share_materials

_VALID = """
export SHARE_WORKSPACE_DOMAIN=host-aaaa.bbbb.us1.imbueminds.com
export SHARE_RELAY_ENDPOINT=relay-us1.infra.imbue.com:7000
export SHARE_RELAY_TOKEN="tok-123"
export SHARE_CONNECTOR_URL=https://connector.example.com/
export SHARE_BROKER_URL='https://accounts.example.com'
"""


def test_parse_share_materials_reads_all_fields() -> None:
    materials = parse_share_materials(_VALID)
    assert materials is not None
    assert materials.workspace_domain == "host-aaaa.bbbb.us1.imbueminds.com"
    assert materials.relay_host == "relay-us1.infra.imbue.com"
    assert materials.relay_port == 7000
    assert materials.relay_token == "tok-123"
    assert materials.connector_url == "https://connector.example.com"
    assert materials.broker_url == "https://accounts.example.com"


def test_parse_share_materials_rejects_missing_or_malformed_keys() -> None:
    assert parse_share_materials("") is None
    assert parse_share_materials("export SHARE_WORKSPACE_DOMAIN=x") is None
    assert parse_share_materials(_VALID.replace(":7000", "")) is None
    assert parse_share_materials(_VALID.replace('tok-123', "")) is None


def test_read_share_materials_handles_missing_file(tmp_path: Path) -> None:
    assert read_share_materials(tmp_path / "absent.env") is None
    materials_path = tmp_path / "share.env"
    materials_path.write_text(_VALID)
    materials = read_share_materials(materials_path)
    assert materials is not None
    assert materials.relay_port == 7000


def test_signing_secret_is_created_once_and_reused(tmp_path: Path) -> None:
    secret_path = tmp_path / "signing_key"
    first = load_or_create_signing_secret(secret_path)
    second = load_or_create_signing_secret(secret_path)
    assert first == second
    assert len(first) > 32
    assert (secret_path.stat().st_mode & 0o777) == 0o600


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
