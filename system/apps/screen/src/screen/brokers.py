"""The two connectivity brokers the service asks (docs/system/specs/screen/spec.md section 5.2): the desktop,
reached through the latchkey gateway's ``minds-api-proxy`` as the chat app's notification client reaches it, and the
share gateway's loopback route."""

from collections.abc import Mapping
from typing import Any
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import SecretStr
from pydantic import ValidationError

from imbue.imbue_common.pure import pure
from screen.data_types import IceServer
from screen.data_types import IceServersAnswer
from screen.data_types import MediaCandidate
from screen.data_types import TransportProtocol
from screen.errors import ConnectivityBrokerError
from screen.interfaces import ConnectivityBrokerInterface

# What mngr injects into every agent's environment, and so into the supervised programs': the gateway's address and
# credentials, and the id of the workspace's primary agent, whose host the desktop answers for.
ENV_GATEWAY: Final[str] = "LATCHKEY_GATEWAY"
ENV_GATEWAY_PASSWORD: Final[str] = "LATCHKEY_GATEWAY_PASSWORD"
ENV_GATEWAY_PERMISSIONS_OVERRIDE: Final[str] = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"
ENV_AGENT_ID: Final[str] = "MNGR_AGENT_ID"
_HEADER_PASSWORD: Final[str] = "X-Latchkey-Gateway-Password"
_HEADER_PERMISSIONS_OVERRIDE: Final[str] = "X-Latchkey-Gateway-Permissions-Override"

# The share gateway's loopback port (system/services/share_gateway/README.md) and its route to the Screen service.
SHARE_GATEWAY_CONNECT_INFO_URL: Final[str] = "http://127.0.0.1:8791/connect-info"

# A request is bounded as the aggregator bounds the answer: two seconds for candidates, longer for relay servers.
BROKER_TIMEOUT_SECONDS: Final[float] = 2.0
ICE_SERVERS_TIMEOUT_SECONDS: Final[float] = 8.0


@pure
def parse_candidates(entries: Any) -> tuple[MediaCandidate, ...]:
    """The candidates of a broker's answer, protocol compared case-insensitively (the desktop spells it upper-case,
    the connector lower-case); an entry off the shape is skipped."""
    if not isinstance(entries, list):
        raise ConnectivityBrokerError("the broker answered no candidate list")
    candidates: list[MediaCandidate] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        protocol_text = str(entry.get("protocol", "")).upper()
        if protocol_text not in TransportProtocol.__members__:
            continue
        try:
            candidate = MediaCandidate(
                host=str(entry.get("host", "")),
                port=int(entry.get("port")),
                protocol=TransportProtocol[protocol_text],
                guest_port=int(entry["guest_port"]) if entry.get("guest_port") is not None else None,
            )
        except (TypeError, ValueError, ValidationError):
            continue
        if candidate.host and 0 < candidate.port < 65536:
            candidates.append(candidate)
    return tuple(candidates)


@pure
def parse_ice_servers_answer(document: Any) -> IceServersAnswer:
    """The ``{"ice_servers", "expires_at"}`` shape both brokers answer; an entry off the shape is skipped."""
    if not isinstance(document, dict):
        raise ConnectivityBrokerError("the broker answered no ICE servers document")
    raw_servers = document.get("ice_servers")
    if not isinstance(raw_servers, list):
        raise ConnectivityBrokerError("the broker answered no ice_servers list")
    servers: list[IceServer] = []
    for entry in raw_servers:
        if not isinstance(entry, dict):
            continue
        try:
            servers.append(IceServer.model_validate(entry))
        except ValidationError:
            continue
    expires_at = document.get("expires_at")
    return IceServersAnswer(
        ice_servers=tuple(servers), expires_at=expires_at if isinstance(expires_at, str) and expires_at else None
    )


def _get_json(url: str, headers: dict[str, str], broker_name: str, timeout_seconds: float) -> Any:
    try:
        response = httpx.get(url, headers=headers, timeout=timeout_seconds)
    except httpx.HTTPError as e:
        raise ConnectivityBrokerError(f"the {broker_name} could not be reached: {e}") from e
    if response.status_code != 200:
        raise ConnectivityBrokerError(f"the {broker_name} answered {response.status_code}: {response.text[:200]}")
    try:
        return response.json()
    except ValueError as e:
        raise ConnectivityBrokerError(f"the {broker_name} answered no JSON") from e


class DesktopBroker(ConnectivityBrokerInterface):
    """The desktop's agent-reachable routes (the integration spec, section 4) through the latchkey gateway."""

    name: str = Field(default="desktop", frozen=True, description="What the broker is called in logs")
    gateway_url: str = Field(frozen=True, description="The latchkey gateway's address")
    password: SecretStr = Field(frozen=True, description="The gateway's listen password")
    permissions_override: SecretStr | None = Field(
        frozen=True, description="The permissions-override JWT a desktop-hosted gateway needs; None on a VPS gateway"
    )
    agent_id: str = Field(frozen=True, description="The workspace's primary agent, whose host the desktop answers for")

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "DesktopBroker | None":
        """The broker the environment names, or None when the gateway or the agent id is missing (a test, a
        workspace started outside the desktop)."""
        gateway_url = environ.get(ENV_GATEWAY, "")
        password = environ.get(ENV_GATEWAY_PASSWORD, "")
        agent_id = environ.get(ENV_AGENT_ID, "")
        if not gateway_url or not password or not agent_id:
            return None
        permissions_override = environ.get(ENV_GATEWAY_PERMISSIONS_OVERRIDE, "")
        return cls(
            gateway_url=gateway_url,
            password=SecretStr(password),
            permissions_override=SecretStr(permissions_override) if permissions_override else None,
            agent_id=agent_id,
        )

    def _url(self, route: str) -> str:
        return f"{self.gateway_url.rstrip('/')}/minds-api-proxy/api/v1/agents/{self.agent_id}/{route}"

    def _headers(self) -> dict[str, str]:
        headers = {_HEADER_PASSWORD: self.password.get_secret_value()}
        if self.permissions_override is not None:
            headers[_HEADER_PERMISSIONS_OVERRIDE] = self.permissions_override.get_secret_value()
        return headers

    def fetch_candidates(self) -> tuple[MediaCandidate, ...]:
        document = _get_json(self._url("media-endpoints"), self._headers(), self.name, BROKER_TIMEOUT_SECONDS)
        if not isinstance(document, dict):
            raise ConnectivityBrokerError("the desktop answered no media endpoints document")
        return parse_candidates(document.get("candidates"))

    def fetch_ice_servers(self) -> IceServersAnswer:
        return parse_ice_servers_answer(
            _get_json(self._url("ice-servers"), self._headers(), self.name, ICE_SERVERS_TIMEOUT_SECONDS)
        )


class ShareGatewayBroker(ConnectivityBrokerInterface):
    """The share gateway's loopback ``GET /connect-info`` (spec section 15), which answers candidates and ICE servers
    at once; an unshared workspace has no gateway listening, which reads as the broker being absent."""

    name: str = Field(default="share gateway", frozen=True, description="What the broker is called in logs")
    url: str = Field(default=SHARE_GATEWAY_CONNECT_INFO_URL, frozen=True, description="The loopback route")

    def _fetch(self, timeout_seconds: float) -> dict[str, Any]:
        document = _get_json(self.url, {}, self.name, timeout_seconds)
        if not isinstance(document, dict):
            raise ConnectivityBrokerError("the share gateway answered no connect-info document")
        return document

    def fetch_candidates(self) -> tuple[MediaCandidate, ...]:
        return parse_candidates(self._fetch(BROKER_TIMEOUT_SECONDS).get("candidates"))

    def fetch_ice_servers(self) -> IceServersAnswer:
        return parse_ice_servers_answer(self._fetch(ICE_SERVERS_TIMEOUT_SECONDS))


def brokers_from_environ(environ: Mapping[str, str]) -> tuple[ConnectivityBrokerInterface, ...]:
    """The brokers this workspace can have: the desktop when the environment names a gateway, the share gateway
    always (it answers only while the workspace is shared)."""
    brokers: list[ConnectivityBrokerInterface] = []
    desktop = DesktopBroker.from_environ(environ)
    if desktop is None:
        logger.info("No latchkey gateway or agent id in the environment; connect-info will not ask the desktop")
    else:
        brokers.append(desktop)
    brokers.append(ShareGatewayBroker())
    return tuple(brokers)
