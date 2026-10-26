"""HTTP endpoint for ``POST /api/chats/<chat_id>/web-lookup``: one web search on a chat's own provider and model.

The onboarding agent's look-up script (``.agents/shared/onboarding-agent/scripts/lookup_user.py``) asks here, so it
names no provider: the chat app finds the chat's account, builds the web look-up its harness registers
(``harnesses/web_lookup.py``), and runs it on the model the chat is set to. The answer is the model's text, read by
the caller; nothing is added to the chat's transcript and nothing the search reads reaches a file or a shell.
"""

from __future__ import annotations

from typing import Final

from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from flask import Flask
from flask import Response
from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.chat.harnesses.web_lookup import WebLookupError
from imbue.chat.harnesses.web_lookup import WebLookupTimeoutError
from imbue.chat.primitives import ChatId
from imbue.chat.state import get_state
from imbue.imbue_common.frozen_model import FrozenModel

logger = _loguru_logger

MAX_SYSTEM_PROMPT_LENGTH: Final[int] = 20_000
MAX_PROMPT_LENGTH: Final[int] = 2_000


class WebLookupBody(FrozenModel):
    """Request body for POST /api/chats/{chat_id}/web-lookup: what to search for, and how to answer."""

    system_prompt: str = Field(
        min_length=1, max_length=MAX_SYSTEM_PROMPT_LENGTH, description="Frames the task and the shape of the answer"
    )
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH, description="What to look up")


class WebLookupResponse(FrozenModel):
    """The model's answer to a web look-up."""

    answer: str = Field(description="The model's answer text")


def web_lookup(chat_id: str) -> Response:
    """Run one web search for ``prompt`` on the chat's provider and model, and answer the model's text.

    404 for a chat with no account to ask on, 501 for a provider that cannot search the web headlessly, 502 when
    the search could not run or gave no answer, 504 when it ran out of time, and 503 until the agent list is read.
    """
    body = parse_request_body(WebLookupBody)
    agent_manager = get_state().agent_manager
    if not agent_manager.is_agent_list_known():
        return error_response("The chat app has not read its agent list from mngr yet; try again shortly.", 503)
    binding = agent_manager.resolve_chat_account_binding(ChatId(chat_id))
    if binding is None:
        return error_response(f"Chat '{chat_id}' was not found, or has no account to search on", 404)
    lookup = get_state().web_lookup_builder(binding.harness)
    if lookup is None:
        return error_response(f"A chat on {binding.harness.value} cannot run a web look-up yet", 501)
    model = agent_manager.chat_model_switch_id(ChatId(chat_id))
    try:
        answer = lookup.search(binding.account_dir, body.system_prompt, body.prompt, model)
    except WebLookupTimeoutError as e:
        logger.warning("The web look-up for chat {} timed out: {}", chat_id, e)
        return error_response(str(e), 504)
    except WebLookupError as e:
        logger.warning("The web look-up for chat {} failed: {}", chat_id, e)
        return error_response(str(e), 502)
    return model_response(WebLookupResponse(answer=answer))


ROUTE_TABLE: Final[RouteTable] = {
    ("POST", "/api/chats/{chat_id}/web-lookup"): RouteModels(
        request_model=WebLookupBody, response_model=WebLookupResponse
    ),
}


def register_routes(application: Flask) -> None:
    """Wire the web look-up route onto the Flask application."""
    application.add_url_rule("/api/chats/<chat_id>/web-lookup", view_func=web_lookup, methods=["POST"])
