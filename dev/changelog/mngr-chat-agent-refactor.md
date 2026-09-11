Added the chat-agent-split plan (`docs/system/blueprint/chat-agent-split/plan-chat-agent-split.md`): the design for making a chat an ordered sequence of one or more mngr agents, so a user can switch harness or account inside an existing chat instead of opening a new one.

The plan settles the vocabulary (chat versus agent, handoff versus rebind), the chat record store, chat-id addressing through the chat app, the multi-segment transcript, the durable handoff sequence with its summary contract, and the seven phases the work lands in.
No behavior changes in this entry; it is documentation only.
