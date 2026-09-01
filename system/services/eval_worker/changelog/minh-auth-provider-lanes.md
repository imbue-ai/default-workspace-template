Resolve the chat agent by its `user_created` label instead of the `initial_chat_agent_id`
sidecar, which bootstrap no longer writes. A chat runs on a provider account and a fresh
workspace has none, so there is no chat at boot to record -- the first one is whichever the
user starts.
