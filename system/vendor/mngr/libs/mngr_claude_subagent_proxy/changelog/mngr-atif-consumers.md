When a subagent is destroyed before it finishes, the proxy recovers its final
message from the preserved common transcript. That reader now understands the
ATIF-shaped records the claude emitter writes (a `step` with `source: "agent"`,
whose text is `message`) as well as the legacy `assistant_message` records that
streams preserved before the cutover still carry.
