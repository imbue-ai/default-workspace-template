from pydantic import AnyHttpUrl
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.share_relay.primitives import DEFAULT_HEALTHCHECK_PORT
from imbue.share_relay.primitives import DEFAULT_TUNNEL_CONTROL_PORT
from imbue.share_relay.primitives import DEFAULT_VHOST_HTTPS_PORT
from imbue.share_relay.primitives import RegionCode
from imbue.share_relay.primitives import RelayPort


class RelayConfiguration(FrozenModel):
    """Everything needed to render one relay host's frps + firewall config.

    A relay is a single small VPS running frps in SNI-passthrough mode: it
    reads the ClientHello SNI of each inbound TLS connection and splices the
    raw byte stream into the matching workspace tunnel, never terminating TLS.
    Every ``NewProxy`` / ``Login`` operation is authorized by a callback to the
    connector, so the relay holds no per-share state and no plaintext.
    """

    region: RegionCode = Field(
        description="Region code -- the label under the content domain apex this relay serves (e.g. 'us1')"
    )
    content_domain: str = Field(
        description="The content domain apex workspace hostnames live under (e.g. 'imbueminds.com')"
    )
    plugin_auth_url: AnyHttpUrl = Field(
        description="Connector endpoint the frps server-plugin calls to authorize Login / NewProxy operations"
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
