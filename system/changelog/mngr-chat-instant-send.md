The shared pulsing activity dot (`activityDotClass` in `system/libs/workspace_ui`) takes an optional tone, `"accent"` (the default, unchanged) or `"warning"`, so a caller can pulse in the warning colour without restating the pulse timing. The chat's new "Connecting…" indicator is the first caller.

The chat-agent-split plan now describes the messages a switching chat holds as the faded, uncaptioned bubble of any send, matching the chat app, which no longer captions a not-yet-delivered message "Sending…", and its wire snapshot lists the new `active_agent.is_connecting`.
