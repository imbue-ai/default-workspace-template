"""Tests for the telegram bot's agent-delivery path.

The ``mngr message`` argv this bot emits is contract-checked against the live
mngr CLI in the repo-root ``test_mngr_cli_argv_contract.py``; here we cover the
telegram-specific behaviour: the message *body* the agent receives carries the
sender username, chat id, and original text so the agent has the context it
needs to reply.
"""

from __future__ import annotations

from telegram_bot.bot import _format_agent_message


def test_format_agent_message_includes_sender_context() -> None:
    body = _format_agent_message(username="alice", text="deploy please", chat_id=42)
    assert body == "telegram message from @alice (chat_id=42): deploy please"
