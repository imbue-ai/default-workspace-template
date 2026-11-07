from pathlib import Path

import pytest
from pydantic import SecretStr

from auth_sandbox.environment import (
    DEFAULT_GEO,
    RegionGeo,
    fingerprint_seed,
    geo_for_region,
    sandbox_identity,
    sign_in_environment,
)
from auth_sandbox.primitives import REGION_ENV_VAR
from auth_sandbox.session import SessionSettings


@pytest.mark.parametrize(
    ("region", "expected"),
    [
        ("us-east-1", RegionGeo(timezone="America/New_York", country="US")),
        ("us1", RegionGeo(timezone="America/Los_Angeles", country="US")),
        ("us2", RegionGeo(timezone="America/New_York", country="US")),
        ("us-west", RegionGeo(timezone="America/Los_Angeles", country="US")),
        ("US1", RegionGeo(timezone="America/Los_Angeles", country="US")),
        ("us3", RegionGeo(timezone="America/New_York", country="US")),
        ("eu-west-1", RegionGeo(timezone="Europe/Dublin", country="IE")),
        ("eu1", RegionGeo(timezone="Europe/Amsterdam", country="NL")),
        ("ap-northeast-1", RegionGeo(timezone="Asia/Tokyo", country="JP")),
        ("mars", DEFAULT_GEO),
        (None, DEFAULT_GEO),
    ],
)
def test_geo_for_region_takes_the_longest_matching_prefix(
    region: str | None, expected: RegionGeo
) -> None:
    assert geo_for_region(region) == expected


def test_fingerprint_seed_is_a_stable_decimal_per_workspace() -> None:
    seed = fingerprint_seed("host-0123456789abcdef0123456789abcdef")
    assert seed == fingerprint_seed("host-0123456789abcdef0123456789abcdef")
    assert seed.isdigit()
    assert seed != fingerprint_seed("host-fedcba9876543210fedcba9876543210")
    assert fingerprint_seed(None).isdigit()


def test_sandbox_identity_takes_the_sessions_timezone_and_seed_and_the_regions_country() -> (
    None
):
    environ = {REGION_ENV_VAR: "us-west-2", "PATH": "/usr/bin"}
    identity = sandbox_identity(
        environ,
        SessionSettings(workspace_host_id="host-abc", timezone="Europe/Paris"),
    )
    assert identity.region == "us-west-2"
    assert identity.workspace_host_id == "host-abc"
    assert identity.geo == RegionGeo(timezone="Europe/Paris", country="US")

    without_timezone = sandbox_identity(
        environ, SessionSettings(workspace_host_id=None, timezone=None)
    )
    assert without_timezone.workspace_host_id is None
    assert without_timezone.geo == RegionGeo(
        timezone="America/Los_Angeles", country="US"
    )

    bare = sandbox_identity(
        {REGION_ENV_VAR: ""}, SessionSettings(workspace_host_id=None, timezone=None)
    )
    assert bare.region is None
    assert bare.geo == DEFAULT_GEO


def test_sign_in_environment_pins_the_launcher_and_the_sandbox_store_and_drops_the_gateway() -> (
    None
):
    base = {
        "PATH": "/usr/bin",
        "LATCHKEY_GATEWAY": "http://host.docker.internal:1989",
        "LATCHKEY_GATEWAY_PASSWORD": "inherited",
    }
    identity = sandbox_identity(
        {REGION_ENV_VAR: "eu-west-1"},
        SessionSettings(workspace_host_id="host-abc", timezone=None),
    )

    environment = sign_in_environment(
        base, identity, Path("/run/auth-sandbox/latchkey"), SecretStr("k3y")
    )

    assert "LATCHKEY_GATEWAY" not in environment
    assert environment["PATH"] == "/usr/bin"
    assert environment["LATCHKEY_GATEWAY_PASSWORD"] == "inherited"
    assert environment["DISPLAY"] == ":99"
    assert environment["XDG_RUNTIME_DIR"] == "/tmp/screen-runtime"
    assert environment["PULSE_SERVER"] == "unix:/tmp/screen-runtime/pulse.sock"
    assert environment["LATCHKEY_DIRECTORY"] == "/run/auth-sandbox/latchkey"
    assert environment["LATCHKEY_ENCRYPTION_KEY"] == "k3y"
    assert environment["LATCHKEY_DISABLE_COUNTING"] == "1"
    assert environment["TILION_NO_ACTIVATE"] == "1"
    assert environment["TILION_TZ"] == "Europe/Dublin"
    assert environment["TILION_COUNTRY"] == "IE"
    assert environment["TILION_SEED"] == fingerprint_seed("host-abc")
    assert base == {
        "PATH": "/usr/bin",
        "LATCHKEY_GATEWAY": "http://host.docker.internal:1989",
        "LATCHKEY_GATEWAY_PASSWORD": "inherited",
    }
