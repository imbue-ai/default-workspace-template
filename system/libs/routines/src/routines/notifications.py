"""The Imbue Studio feed, reached through the latchkey gateway as the notify-user script reaches it."""

from typing import Final

import httpx
from loguru import logger
from pydantic import Field

from routines.interfaces import NotifierInterface
from routines.primitives import RoutineId

GATEWAY_ENV: Final[str] = "LATCHKEY_GATEWAY"
GATEWAY_PASSWORD_ENV: Final[str] = "LATCHKEY_GATEWAY_PASSWORD"
GATEWAY_PERMISSIONS_ENV: Final[str] = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"
AGENT_ID_ENV: Final[str] = "MNGR_AGENT_ID"
# The workspace message the desktop delivers when the entry is opened; the Routines app's manifest handles it.
SHOW_ROUTINE_MESSAGE_TYPE: Final[str] = "minds:show-routine"
NOTIFY_TIMEOUT_SECONDS: Final[float] = 10.0


class GatewayNotifier(NotifierInterface):
    """Posts to the app's per-agent notifications route as the services agent."""

    environ: dict[str, str] = Field(frozen=True, description="The process environment the gateway is named in")

    def notify(self, routine_id: RoutineId, title: str, message: str) -> bool:
        gateway = self.environ.get(GATEWAY_ENV, "")
        password = self.environ.get(GATEWAY_PASSWORD_ENV, "")
        agent_id = self.environ.get(AGENT_ID_ENV, "")
        if not gateway or not password or not agent_id:
            logger.warning("Skipped a notification: the latchkey gateway environment is not set")
            return False
        headers = {
            "Content-Type": "application/json",
            "X-Latchkey-Gateway-Password": password,
        }
        permissions = self.environ.get(GATEWAY_PERMISSIONS_ENV, "")
        if permissions:
            headers["X-Latchkey-Gateway-Permissions-Override"] = permissions
        payload = {
            "title": title,
            "message": message,
            "destination": {
                "type": SHOW_ROUTINE_MESSAGE_TYPE,
                "routine_id": str(routine_id),
            },
        }
        url = f"{gateway.rstrip('/')}/minds-api-proxy/api/v1/agents/{agent_id}/notifications"
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=NOTIFY_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            logger.warning(
                "Skipped a notification: the Imbue Studio app could not be reached ({})",
                e,
            )
            return False
        if response.status_code != 200:
            logger.warning(
                "Skipped a notification: the Imbue Studio app answered {}",
                response.status_code,
            )
            return False
        return True
