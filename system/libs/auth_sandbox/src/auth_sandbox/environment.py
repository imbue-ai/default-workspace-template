"""What the sandbox knows about itself from the environment the connector gave it, and the environment a
sign-in runs under.

The connector passes nothing secret at create (D4): the sandbox id, the connector URL, the sandbox's host id,
its region and the workspace's host id come as ``AUTH_SANDBOX_*`` variables. The sign-in's Fortress launcher
needs a timezone and a country pinned from the region (so no geo lookup sits on the critical path) and a
fingerprint seed derived from the workspace, which is what this module derives.
"""

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.encryption_key import LATCHKEY_ENCRYPTION_KEY_ENV_VAR
from pydantic import Field, SecretStr

from auth_sandbox.primitives import (
    COUNTRY_ENV_VAR,
    DISPLAY_ENV_VAR,
    LATCHKEY_DIRECTORY_ENV_VAR,
    LATCHKEY_DISABLE_COUNTING_ENV_VAR,
    LATCHKEY_GATEWAY_ENV_VAR,
    PULSE_SERVER_ENV_VAR,
    REGION_ENV_VAR,
    SCREEN_DISPLAY,
    SCREEN_PULSE_SERVER,
    SCREEN_RUNTIME_DIR,
    TILION_COUNTRY_ENV_VAR,
    TILION_NO_ACTIVATE_ENV_VAR,
    TILION_SEED_ENV_VAR,
    TILION_TZ_ENV_VAR,
    TIMEZONE_ENV_VAR,
    WORKSPACE_HOST_ID_ENV_VAR,
    XDG_RUNTIME_DIR_ENV_VAR,
)


class RegionGeo(FrozenModel):
    """The timezone and country the launcher is pinned to for a region."""

    timezone: str = Field(description="An IANA zone name")
    country: str = Field(description="An ISO 3166-1 alpha-2 code")


# A region name (a Modal region, or the connector's own region label) is matched by its first letters, longest
# prefix first; a region none of these match gets UTC and the US, the launcher's own defaults when told nothing.
_GEO_BY_REGION_PREFIX: Final[tuple[tuple[str, RegionGeo], ...]] = (
    ("us-west", RegionGeo(timezone="America/Los_Angeles", country="US")),
    ("us-east", RegionGeo(timezone="America/New_York", country="US")),
    ("us", RegionGeo(timezone="America/New_York", country="US")),
    ("ca", RegionGeo(timezone="America/Toronto", country="CA")),
    ("eu-west", RegionGeo(timezone="Europe/Dublin", country="IE")),
    ("eu-central", RegionGeo(timezone="Europe/Berlin", country="DE")),
    ("eu", RegionGeo(timezone="Europe/Amsterdam", country="NL")),
    ("uk", RegionGeo(timezone="Europe/London", country="GB")),
    ("ap-northeast", RegionGeo(timezone="Asia/Tokyo", country="JP")),
    ("ap-southeast", RegionGeo(timezone="Asia/Singapore", country="SG")),
    ("ap-south", RegionGeo(timezone="Asia/Kolkata", country="IN")),
    ("ap", RegionGeo(timezone="Asia/Singapore", country="SG")),
    ("au", RegionGeo(timezone="Australia/Sydney", country="AU")),
    ("sa", RegionGeo(timezone="America/Sao_Paulo", country="BR")),
    ("jp", RegionGeo(timezone="Asia/Tokyo", country="JP")),
    ("in", RegionGeo(timezone="Asia/Kolkata", country="IN")),
)
DEFAULT_GEO: Final[RegionGeo] = RegionGeo(timezone="UTC", country="US")

# How many bytes of the workspace host id's digest make the launcher's seed: a decimal integer that fits
# whatever width the launcher parses a seed as, stable for the workspace's life.
_SEED_DIGEST_BYTES: Final[int] = 4


class SandboxIdentity(FrozenModel):
    """What the sign-in needs to know about this sandbox, read from the connector's environment."""

    region: str | None = Field(
        description="The region the connector created the sandbox in, if it said"
    )
    workspace_host_id: str | None = Field(
        description="The mngr host id of the workspace being signed in"
    )
    geo: RegionGeo = Field(description="The launcher's timezone and country pins")


@pure
def geo_for_region(region: str | None) -> RegionGeo:
    """The launcher pins a region implies: the longest matching prefix wins; an unknown region gets the default."""
    if region is None:
        return DEFAULT_GEO
    normalized = region.strip().lower()
    for prefix, geo in sorted(_GEO_BY_REGION_PREFIX, key=lambda entry: -len(entry[0])):
        if normalized.startswith(prefix):
            return geo
    return DEFAULT_GEO


@pure
def fingerprint_seed(workspace_host_id: str | None) -> str:
    """The launcher's ``TILION_SEED`` for a workspace: one stable decimal per workspace, so a service sees the same
    device across that workspace's sign-ins. A sandbox that was told no workspace seeds from the empty string."""
    digest = hashlib.sha256((workspace_host_id or "").encode("utf-8")).digest()
    return str(int.from_bytes(digest[:_SEED_DIGEST_BYTES], "big"))


@pure
def identity_from_environ(environ: Mapping[str, str]) -> SandboxIdentity:
    """Read the connector's environment; an explicit timezone or country override beats what the region implies."""
    region = environ.get(REGION_ENV_VAR) or None
    implied = geo_for_region(region)
    geo = RegionGeo(
        timezone=environ.get(TIMEZONE_ENV_VAR) or implied.timezone,
        country=environ.get(COUNTRY_ENV_VAR) or implied.country,
    )
    return SandboxIdentity(
        region=region,
        workspace_host_id=environ.get(WORKSPACE_HOST_ID_ENV_VAR) or None,
        geo=geo,
    )


@pure
def sign_in_environment(
    base: Mapping[str, str],
    identity: SandboxIdentity,
    latchkey_directory: Path,
    latchkey_key: SecretStr,
) -> dict[str, str]:
    """The environment ``latchkey auth browser`` and the Fortress it launches run under.

    On top of ``base``: the Screen's display and audio, the sandbox's latchkey directory and key (the key as the
    operator override ``imbue.mngr_latchkey`` honours, so nothing is minted on disk), counting off, and the
    launcher's pins. ``LATCHKEY_GATEWAY`` is dropped: the sandbox has no gateway, and a local ``latchkey``
    invocation that sees one would try to use it.
    """
    environment = {
        name: value for name, value in base.items() if name != LATCHKEY_GATEWAY_ENV_VAR
    }
    environment.update(
        {
            DISPLAY_ENV_VAR: SCREEN_DISPLAY,
            XDG_RUNTIME_DIR_ENV_VAR: SCREEN_RUNTIME_DIR,
            PULSE_SERVER_ENV_VAR: SCREEN_PULSE_SERVER,
            LATCHKEY_DIRECTORY_ENV_VAR: str(latchkey_directory),
            LATCHKEY_ENCRYPTION_KEY_ENV_VAR: latchkey_key.get_secret_value(),
            LATCHKEY_DISABLE_COUNTING_ENV_VAR: "1",
            TILION_NO_ACTIVATE_ENV_VAR: "1",
            TILION_TZ_ENV_VAR: identity.geo.timezone,
            TILION_COUNTRY_ENV_VAR: identity.geo.country,
            TILION_SEED_ENV_VAR: fingerprint_seed(identity.workspace_host_id),
        }
    )
    return environment
