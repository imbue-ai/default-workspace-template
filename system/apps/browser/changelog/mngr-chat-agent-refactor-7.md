Phase 7 of the chat-agent split: a browser is owned by the chat that holds it, not by the agent that claimed it.

- The fleet CLI (`agentic-browser-fleet`) sends the caller's chat id as the owner (`MINDS_CHAT_ID` on an agent the chat app created, else `MNGR_AGENT_ID`, so a background agent names itself as before), and the daemon's wake-ups and nudges reach that chat through the in-workspace messenger, which delivers to whichever agent runs the chat now. "you" versus "agent <name>" in the fleet's listings compares the same id.
