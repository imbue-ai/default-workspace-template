import re
from typing import Final

from pydantic import AnyHttpUrl
from pydantic import Field
from pydantic import SecretStr
from pydantic import field_validator

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.share_relay.errors import InvalidRelayConfigurationError
from imbue.share_relay.primitives import ContentDomain
from imbue.share_relay.primitives import DEFAULT_HEALTHCHECK_PORT
from imbue.share_relay.primitives import DEFAULT_TUNNEL_CONTROL_PORT
from imbue.share_relay.primitives import DEFAULT_VHOST_HTTPS_PORT
from imbue.share_relay.primitives import RegionCode
from imbue.share_relay.primitives import RelayId
from imbue.share_relay.primitives import RelayPort

# The plugin secret is rendered unencoded into the plugin addr's URL userinfo,
# so it must be userinfo-safe: a character like '@', ':' or '/' would corrupt
# the rendered frps config into one whose auth callbacks silently fail.
# `openssl rand -hex 32` output (the documented generation command) fits.
_PLUGIN_AUTH_SECRET_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class RelayConfiguration(FrozenModel):
    """Everything needed to render one relay host's frps + firewall config.

    A relay is a single small VPS running frps in SNI-passthrough mode: it
    reads the ClientHello SNI of each inbound TLS connection and splices the
    raw byte stream into the matching workspace tunnel, never terminating TLS.
    Every ``NewProxy`` / ``Login`` operation is authorized by a callback to the
    connector, so the relay holds no per-share state and no plaintext.
    """

    relay_id: RelayId = Field(
        description=(
            "This relay's registered identity (relay-<hex>); appended to the plugin-auth path so the "
            "connector can attribute Login/NewProxy callbacks per relay"
        )
    )
    region: RegionCode = Field(
        description="Region code -- the label under the content domain apex this relay serves (e.g. 'us1')"
    )
    content_domain: ContentDomain = Field(
        description="The content domain apex workspace hostnames live under (e.g. 'imbueminds.com')"
    )
    plugin_auth_url: AnyHttpUrl = Field(
        description=(
            "Connector endpoint the frps server-plugin calls to authorize Login / NewProxy operations "
            "(secret-free; the secret is carried separately in plugin_auth_secret)"
        )
    )
    plugin_auth_secret: SecretStr = Field(
        description=(
            "Shared secret authenticating plugin callbacks to the connector; rendered as the plugin "
            "addr's URL userinfo, which frps's HTTP client delivers as an Authorization: Basic header "
            "(so the secret never appears in the connector's access-logged URL path)"
        )
    )
    vhost_https_port: RelayPort = Field(
        default=DEFAULT_VHOST_HTTPS_PORT,
        description="SNI-passthrough vhost port browsers reach shared workspaces on",
    )
    tunnel_control_port: RelayPort = Field(
        default=DEFAULT_TUNNEL_CONTROL_PORT,
        description="frps tunnel control port frpc dials outbound from each workspace",
    )
    healthcheck_port: RelayPort = Field(
        default=DEFAULT_HEALTHCHECK_PORT,
        description="Internal healthcheck HTTP port (never exposed to workspace traffic)",
    )
    max_new_connections_per_second_per_ip: int = Field(
        default=20,
        description="nftables new-connection rate limit per source IP on the vhost port (tier-2 abuse guard)",
    )
    max_new_connections_burst_per_ip: int = Field(
        default=40,
        description="nftables new-connection burst allowance per source IP",
    )
    max_concurrent_connections_per_ip: int = Field(
        default=100,
        description="nftables concurrent-connection cap per source IP on the vhost port",
    )

    @field_validator("plugin_auth_url")
    @classmethod
    def _plugin_auth_url_is_a_bare_origin_and_path(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        # The frps config renderer keeps only the URL's origin and path (frps
        # concatenates addr + path itself and appends its own query), so a
        # query/fragment would be silently dropped -- e.g. a secret passed as
        # ?secret=... would render a relay whose auth callbacks all fail. And
        # the renderer inserts plugin_auth_secret as the addr's userinfo, so
        # userinfo in the URL itself would collide with that insertion.
        if value.query or value.fragment:
            raise InvalidRelayConfigurationError(
                "plugin_auth_url must not carry a query string or fragment; "
                "the plugin secret is supplied separately via plugin_auth_secret"
            )
        if value.username or value.password:
            raise InvalidRelayConfigurationError(
                "plugin_auth_url must not carry userinfo; "
                "the plugin secret is supplied separately via plugin_auth_secret"
            )
        return value

    @field_validator("plugin_auth_secret")
    @classmethod
    def _plugin_auth_secret_is_userinfo_safe(cls, value: SecretStr) -> SecretStr:
        if _PLUGIN_AUTH_SECRET_RE.match(value.get_secret_value()) is None:
            raise InvalidRelayConfigurationError(
                "plugin_auth_secret must be 16..128 characters of [A-Za-z0-9_-] (generate with `openssl rand -hex 32`)"
            )
        return value

    @property
    def region_domain(self) -> str:
        """The registrable base for this region, e.g. ``us1.imbueminds.com``.

        Every workspace hostname this relay serves is a subdomain of this, and
        this is the Public-Suffix-List entry that makes each user's workspaces
        their own site.
        """
        return f"{self.region}.{self.content_domain}"

    @property
    def vhost_wildcard(self) -> str:
        """The wildcard ``customDomains`` pattern this relay's tunnels register under."""
        return f"*.{self.region_domain}"
