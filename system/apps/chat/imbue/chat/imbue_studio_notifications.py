"""What the chat app tells the Imbue Studio app's notification feed: that the user has just read a chat.

When a chat goes from unwatched to watched (``presence.py``), the user is reading it now, so
the app's unread notifications from it are read too. The chat app posts
``POST /api/v1/agents/<agent id>/notifications/read`` with an empty body to the app through the
latchkey gateway's ``minds-api-proxy``, the same route family ``notify_user.py`` posts a
notification to, addressed as the chat's current agent (the app files a chat's notifications by
the agent's ``chat_id`` label, so any agent of the chat names it).

Best-effort and off the request thread: the posts run one at a time on a single worker, so a
presence report never waits on the gateway. An app from before the route answers 404 and an
unreachable gateway raises; both are logged at debug and dropped, since nothing is lost that
the user cannot clear by hand.
"""

from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.latchkey_gateway import GatewayAccess
from imbue.chat.primitives import ChatId
from imbue.imbue_common.mutable_model import MutableModel

# One round trip to the desktop app through the gateway.
_READ_TIMEOUT_SECONDS: Final[float] = 10.0


class ImbueStudioNotificationsClient(MutableModel):
    """Posts the chat app's calls to the Imbue Studio app's notification feed, one at a time, off the caller's thread."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    gateway: GatewayAccess | None = Field(
        frozen=True,
        description="How to reach the app; None when the gateway env is absent (a secondary chat, a test), which "
        "drops every call",
    )
    resolve_agent_id: Callable[[ChatId], str | None] = Field(
        frozen=True, description="The agent a chat runs on now; None for a chat with no agent yet"
    )

    _executor: ThreadPoolExecutor = PrivateAttr(
        default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="imbue-studio-notifications")
    )

    def mark_chat_read(self, chat_id: ChatId) -> Future[None]:
        """Queue the read call for ``chat_id``; the future settles once it has been posted, or dropped."""
        return self._executor.submit(self._post_chat_read, chat_id)

    def shutdown(self) -> None:
        """Drop the calls not yet started and let the one in flight finish on its own. Idempotent."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _post_chat_read(self, chat_id: ChatId) -> None:
        gateway = self.gateway
        if gateway is None:
            logger.debug("Not marking chat {} read in the Imbue Studio app: no latchkey gateway", chat_id)
            return
        agent_id = self.resolve_agent_id(chat_id)
        if agent_id is None:
            logger.debug("Not marking chat {} read in the Imbue Studio app: it runs on no agent", chat_id)
            return
        try:
            response = httpx.post(
                gateway.url(f"/minds-api-proxy/api/v1/agents/{agent_id}/notifications/read"),
                json={},
                headers=gateway.headers(),
                timeout=_READ_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            logger.debug("Could not mark chat {} read in the Imbue Studio app: {}", chat_id, e)
            return
        if response.status_code == 404:
            logger.debug("The Imbue Studio app has no read route; chat {} stays unread there", chat_id)
        elif not response.is_success:
            logger.warning(
                "The Imbue Studio app answered {} to marking chat {} read: {}",
                response.status_code,
                chat_id,
                response.text[:200],
            )
        else:
            logger.debug("Marked chat {} read in the Imbue Studio app", chat_id)
