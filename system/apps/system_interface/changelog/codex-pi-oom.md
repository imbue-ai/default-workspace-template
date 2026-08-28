The OOM prioritizer resolves an agent's `*_process_started` marker filename from its
`HarnessSpec` instead of from a live activity-tracker instance.

The filename is harness identity, known as soon as the agent is. Reading it off a tracker
meant `_read_agent_process_started_at` returned `None` for any agent whose tracker had not
been registered yet -- and `_ensure_activity_tracking` skips every agent with no local
state dir -- so the prioritizer silently fell back to "no marker" and lost its aging for
exactly the agents it most needs to age. The tracker keeps its own `marker_filename`, which
it uses to bound transcript staleness; a test pins the two to the same value so they cannot
drift.
