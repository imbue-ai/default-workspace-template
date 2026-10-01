New `SPARE_AGENT` band (1000, the ceiling) for a spare chat agent: one the chat app starts ahead of the next new chat and that no one uses yet. The launch wrapper puts an agent labelled `chat_spare=true` (`agent_identity.is_spare_agent`) in it, so the spare and every process it spawns are shed before anything else under memory pressure, after a restart too. Once a chat takes a spare, the chat band policy holds that chat at the engaged floor for its first minute (`CHAT_JUST_STARTED_GRACE_SECONDS`) before scoring it like any chat.

The browser band now tops out at 990 (`SHARED_BROWSER`, down from 1000; the floor stays 910), so renderers still sit at the top of their band but below a spare, which holds no one's work where a renderer costs a tab.

`memory_candidates.py` leaves agents labelled `chat_spare=true` out of its idle chats, since no chat list shows a spare until a chat takes it.
