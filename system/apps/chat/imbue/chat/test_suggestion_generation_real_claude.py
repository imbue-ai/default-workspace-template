"""The suggestion prompt asked through the pinned ``claude -p``, the way the chat app asks it at a turn's end.

Only the model is a stand-in (``StandInAnthropicApi``). The binary is the real one, so this pins what the one-shot
call depends on it for: that it takes the flags and environment the call builds, asks a Sonnet model on the chat's
account, carries the system prompt and the conversation to it, and hands back the model's text alone.
"""

import json
from pathlib import Path

import pytest

from imbue.chat.chat_seed import SeedRole
from imbue.chat.chat_seed import SeedTurn
from imbue.chat.harnesses.claude.one_shot import ClaudeOneShotCompletion
from imbue.chat.harnesses.one_shot import OneShotModelTier
from imbue.chat.suggestion_generation import ConversationEnd
from imbue.chat.suggestion_generation import SUGGESTION_COMPLETION_TIMEOUT_SECONDS
from imbue.chat.suggestion_generation import SUGGESTION_GENERATION_SYSTEM_PROMPT
from imbue.chat.suggestion_generation import SUGGESTION_THINKING_TOKEN_ALLOWANCE
from imbue.chat.suggestion_generation import parse_generated_suggestions
from imbue.chat.suggestion_generation import suggestion_generation_prompt
from imbue.chat.testing import StandInAnthropicApi
from imbue.chat.testing import skip_unless_pinned_claude

pytestmark = [pytest.mark.real_claude]

_STAND_IN_API_KEY = "sk-ant-api03-stand-in-suggestions"
_REPLY = "Plant Watering is built and open beside this chat. Do you want a reminder each morning?"
_ANSWER = (
    '<suggestion recommended="true">Yes, remind me each morning</suggestion>\n<suggestion>No reminders</suggestion>'
)


def test_the_suggestion_prompt_reaches_a_sonnet_model_and_its_answer_reads_back_as_suggestions(tmp_path: Path) -> None:
    skip_unless_pinned_claude()
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    prompt = suggestion_generation_prompt(
        ConversationEnd(
            reply_event_id="evt-reply",
            reply_text=_REPLY,
            is_reply_a_failure=False,
            earlier_turns=(SeedTurn(role=SeedRole.USER, text="Build me an app that tracks my houseplants"),),
        )
    )

    with StandInAnthropicApi(reply_for=lambda _body: _ANSWER) as api:
        # A pasted credential lives in the account's settings ``env``, which is how the call reaches this account.
        (account_dir / "settings.json").write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": api.base_url, "ANTHROPIC_API_KEY": _STAND_IN_API_KEY}})
        )
        answer = ClaudeOneShotCompletion().complete(
            account_dir,
            SUGGESTION_GENERATION_SYSTEM_PROMPT,
            prompt,
            model_tier=OneShotModelTier.MEDIUM,
            thinking_token_allowance=SUGGESTION_THINKING_TOKEN_ALLOWANCE,
            timeout_seconds=SUGGESTION_COMPLETION_TIMEOUT_SECONDS,
        )

    assert [suggestion.label for suggestion in parse_generated_suggestions(answer)] == [
        "Yes, remind me each morning",
        "No reminders",
    ]
    asked = [
        request["body"]
        for request in api.captured
        if request["path"].split("?")[0] == "/v1/messages" and _REPLY in json.dumps(request["body"].get("messages"))
    ]
    assert len(asked) == 1, f"expected one request carrying the conversation, saw {len(asked)}"
    assert "sonnet" in asked[0]["model"]
    assert "Your task is to come up with reasonable" in json.dumps(asked[0]["system"])
    assert asked[0].get("tools", []) == []
