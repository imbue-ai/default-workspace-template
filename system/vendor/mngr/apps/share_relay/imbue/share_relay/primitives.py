import re
from typing import Final

from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import PositiveInt


class RegionCode(NonEmptyStr):
    """A relay region code -- the label directly under the content domain apex.

    Both a DNS label and a Public-Suffix-List entry component, so it must be a
    lowercase DNS label: alphanumeric runs joined by single hyphens, no leading
    or trailing hyphen. Examples: ``us1``, ``us2``, ``eu1``, ``dev-josh-1``.
    """

    def __new__(cls, value: str) -> "RegionCode":
        instance = super().__new__(cls, value)
        if _DNS_LABEL_RE.match(str(instance)) is None:
            raise InvalidPrimitiveValueError(
                f"{cls.__name__} must be a lowercase DNS label (alphanumeric runs joined by single hyphens); "
                f"got {value!r}"
            )
        return instance


class RelayPort(PositiveInt):
    """A TCP port a relay process binds. Must be > 0."""


# A single DNS label: lowercase alphanumeric runs joined by single hyphens, no
# leading/trailing/consecutive hyphens, 1..63 chars.
_DNS_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"^(?=.{1,63}$)[a-z0-9]+(?:-[a-z0-9]+)*$")

# The frps SNI-passthrough vhost port. Browsers reach shared workspaces over
# TLS on 443; frps reads the ClientHello SNI and splices the raw byte stream
# into the matching tunnel without terminating TLS.
DEFAULT_VHOST_HTTPS_PORT: Final[RelayPort] = RelayPort(443)

# The frps tunnel control port. frpc (inside each workspace container) dials
# this outbound to register its tunnel and multiplex traffic.
DEFAULT_TUNNEL_CONTROL_PORT: Final[RelayPort] = RelayPort(7000)

# The relay's healthcheck HTTP port (loopback / internal only; fronted by the
# monitoring probe, never by workspace traffic).
DEFAULT_HEALTHCHECK_PORT: Final[RelayPort] = RelayPort(8080)
