"""The latchkey gateway as the shell's backend reaches it: the address and credentials mngr injects, and the headers.

The same agent<->gateway contract the chat app's ``latchkey_gateway.py`` and the files app's
``gateway.py`` read: the shell runs under supervisord, whose environment is the one mngr gives
the workspace's agents, so it holds the same gateway address and credentials an agent does. The
override JWT is load-bearing on desktop-hosted gateways: a request without it is evaluated
against a deny-all default.
"""

from collections.abc import Mapping
from typing import Final

from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel

ENV_GATEWAY: Final[str] = "LATCHKEY_GATEWAY"
ENV_GATEWAY_PASSWORD: Final[str] = "LATCHKEY_GATEWAY_PASSWORD"
ENV_GATEWAY_PERMISSIONS_OVERRIDE: Final[str] = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"
_HEADER_PASSWORD: Final[str] = "X-Latchkey-Gateway-Password"
_HEADER_PERMISSIONS_OVERRIDE: Final[str] = "X-Latchkey-Gateway-Permissions-Override"


class GatewayAccess(FrozenModel):
    """Where the gateway is and what every request to it carries."""

    base_url: str = Field(min_length=1, description="The gateway's address, as LATCHKEY_GATEWAY names it")
    password: SecretStr = Field(description="The gateway's listen password")
    permissions_override: SecretStr | None = Field(
        description="The permissions-override JWT a desktop-hosted gateway needs; None on a VPS gateway"
    )

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "GatewayAccess | None":
        """The gateway ``environ`` names, or None when the address or the password is missing."""
        base_url = environ.get(ENV_GATEWAY, "")
        password = environ.get(ENV_GATEWAY_PASSWORD, "")
        if not base_url or not password:
            return None
        permissions_override = environ.get(ENV_GATEWAY_PERMISSIONS_OVERRIDE, "")
        return cls(
            base_url=base_url,
            password=SecretStr(password),
            permissions_override=SecretStr(permissions_override) if permissions_override else None,
        )

    def url(self, path: str) -> str:
        """The gateway URL of ``path`` (rooted with one slash)."""
        return f"{self.base_url.rstrip('/')}{path}"

    def headers(self) -> dict[str, str]:
        headers = {_HEADER_PASSWORD: self.password.get_secret_value()}
        if self.permissions_override is not None:
            headers[_HEADER_PERMISSIONS_OVERRIDE] = self.permissions_override.get_secret_value()
        return headers
