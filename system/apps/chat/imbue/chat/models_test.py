from imbue.chat.models import parse_subagent_key
from imbue.chat.primitives import SUBAGENT_KEY_SEPARATOR


def test_a_subagent_key_is_the_chat_the_agent_and_the_session() -> None:
    """A subagent view's key names all three; a chat's own key and the older two-part shape are not one."""
    key = SUBAGENT_KEY_SEPARATOR.join(("agent-1", "agent-2", "sess-3"))
    parsed = parse_subagent_key(key)
    assert parsed is not None
    assert (parsed.chat_id, parsed.agent_id, parsed.session_id) == ("agent-1", "agent-2", "sess-3")
    assert parse_subagent_key("agent-1") is None
    assert parse_subagent_key("agent-1.sess-3") is None
    assert parse_subagent_key("not-an-agent.agent-2.sess-3") is None
    assert parse_subagent_key("agent-1.agent-2.") is None
