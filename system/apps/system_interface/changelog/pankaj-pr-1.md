Separate a chat's stable identity (ChatId) from the id of the mngr agent backing it, with zero behavior change, as the foundation for in-chat harness switching. A chat's id remains its first agent's id, so nothing persisted migrates.

Chat operations gain canonical `/api/chats/<chat_id>/...` routes that resolve the chat's active agent server-side and dispatch to the same handlers as the existing `/api/agents/...` routes, which remain unchanged as the physical/internal contract; the frontend now calls the chat routes.

A durable logical-chat registry (one JSON file per chat beside the saved layouts) records each chat's backing-agent segment, bootstrapped idempotently from discovery; resolution falls back to identity for unrecorded chats.

ChatId types in both languages (a Python NewType, a branded TypeScript type) make mixing chat ids and agent ids a type error while values stay identical; frontend conversation stores re-key to ChatId with byte-identical localStorage keys.

New ratchets confine frontend `/api/agents` literals to the designated physical-contract module and push new backend id-keyed signatures/maps to choose ChatId or AgentId explicitly.
