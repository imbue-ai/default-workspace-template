"""Unit tests for the decider's pure helpers (no network)."""

from __future__ import annotations

from eval_worker import decider


def test_render_conversation_keeps_user_facing_turns() -> None:
    events = [
        {"type": "user_message", "content": "hi what can you do"},
        {"type": "assistant_message", "text": ""},  # internal placeholder, dropped
        {"type": "assistant_message", "text": "I can help with three things."},
        {"type": "user_message", "content": ""},  # empty, dropped
    ]
    assert decider._render_conversation(events) == (
        "YOU (client): hi what can you do\n\nAGENT: I can help with three things."
    )


def test_prompt_includes_persona_and_conversation() -> None:
    prompt = decider._prompt("A busy non-technical founder.", "YOU (client): hi\n\nAGENT: hello")
    assert "A busy non-technical founder." in prompt
    assert "YOU (client): hi" in prompt and "AGENT: hello" in prompt


def test_prompt_without_persona_omits_persona_line() -> None:
    prompt = decider._prompt("", "AGENT: hello")
    assert "persona" not in prompt.lower()
    assert "AGENT: hello" in prompt


def test_decide_falls_back_without_key(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert decider.decide_next_message("agent-1", "persona", "") == decider._FALLBACK
