- tk gains `tk cancel <id> "<reason>"`, which records in a sidecar next to the ticket that a step is no longer wanted, and `tk create --id <id>`, which creates a ticket under a reserved id and never writes over an existing one. A cancel holds until the step is started again: `tk steps` marks the step, `tk show` shows the cancel, and `tk start` says it overrides it.

- A `tk start` or `tk close` of a message's step id now tells the agent when the chat's model judged the message to repeat or call off one of its open steps. The agent is told these decisions come from a quick model that sees one message at a time, and that it may override them: start a cancelled step to keep it, or create a step for a message judged not a task. Another chat's message ids are left to tk (`system/scripts/agent_message_steps.py`).

- AGENTS.md describes these cases. The rule that only one step is in progress at a time is unchanged.
