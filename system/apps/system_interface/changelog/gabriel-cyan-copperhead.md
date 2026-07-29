A chat created from the workspace UI could be left permanently stuck on "No conversation data", even though the agent finished being created a second later and was perfectly healthy. The panel now recovers on its own -- no page reload, no tab switch.

`create-chat` returns as soon as the background `mngr create` starts, and the agent is only registered when that finishes, so the new panel's first transcript fetch races ahead of it and gets a 404. That miss was latched and only ever cleared by the next transcript fetch, which the panel never made again for an agent it had already loaded. The panel now retries that fetch when the agent-state broadcast names the agent -- the same registry the transcript endpoint resolves against, so the retry is guaranteed to land rather than being a blind timer.

The creation build log, which is supposed to cover that window, is not a reliable backstop: the frontend holds the proto-agent only between the `proto_agent_created` and `proto_agent_completed` broadcasts, so any delivery lag longer than the creation itself leaves no render in which the cover is up, and the panel drops straight onto the 404.

Two further fixes on the same path:

The "No conversation data" view captured the agent's terminal once per redraw and redrew on every capture, so the loop only stopped if a capture came back with content. For an agent that has one it settles after a handful of requests; for one with no pane to capture it never stops, and was measured at roughly 170 requests per second, each shelling out to tmux. It now captures once per agent.

A `proto_agent_created` that arrives after its agent is already registered no longer strands the panel on a false "Agent creation failed" screen. The build log is now only shown while the agent is not yet a real agent; asking for a finished creation's log gets a "Proto-agent not found" reply, which the panel used to read as a failed creation and keep on screen indefinitely.
