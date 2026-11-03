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
from typing import Any
from typing import Final

import httpx
from latchkey_access.gateway import GatewayAccess
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

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

    def notify_mention(self, mentioned_in: ChatId, title: str, message: str) -> Future[None]:
        """Queue a notification that the owner was mentioned in ``mentioned_in`` (the multiplayer spec, section
        7.4), posted as that chat's own agent, so a click opens that chat; a chat with no agent sends none."""
        return self._executor.submit(self._post_mention, mentioned_in, title, message)

    def shutdown(self) -> None:
        """Drop the calls not yet started and let the one in flight finish on its own. Idempotent."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _post_mention(self, mentioned_in: ChatId, title: str, message: str) -> None:
        payload = {
            "message": message,
            "title": title,
            "destination": {"type": "minds:focus-chat", "chatId": str(mentioned_in)},
        }
        response = self._post_as_chats_agent(
            mentioned_in, "/notifications", payload, f"notifying the owner of a mention in {mentioned_in}"
        )
        if response is None:
            return
        if not response.is_success:
            logger.warning(
                "The Imbue Studio app answered {} to a mention notification for {}: {}",
                response.status_code,
                mentioned_in,
                response.text[:200],
            )
        else:
            logger.debug("Notified the owner of a mention in {}", mentioned_in)

    def _post_chat_read(self, chat_id: ChatId) -> None:
        response = self._post_as_chats_agent(chat_id, "/notifications/read", {}, f"marking chat {chat_id} read")
        if response is None:
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

    def _post_as_chats_agent(
        self, chat_id: ChatId, route: str, payload: dict[str, Any], purpose: str
    ) -> httpx.Response | None:
        """POST ``payload`` to the app's ``route`` under the agent ``chat_id`` runs on, through the gateway; None,
        logged at debug with ``purpose``, when there is no gateway, the chat runs on no agent, or the post failed."""
        gateway = self.gateway
        if gateway is None:
            logger.debug("Not {} in the Imbue Studio app: no latchkey gateway", purpose)
            return None
        agent_id = self.resolve_agent_id(chat_id)
        if agent_id is None:
            logger.debug("Not {} in the Imbue Studio app: chat {} runs on no agent", purpose, chat_id)
            return None
        try:
            return httpx.post(
                gateway.url(f"/minds-api-proxy/api/v1/agents/{agent_id}{route}"),
                json=payload,
                headers=gateway.headers(),
                timeout=_READ_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            logger.debug("Could not post to the Imbue Studio app ({}): {}", purpose, e)
            return None
