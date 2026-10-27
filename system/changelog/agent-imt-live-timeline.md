- A `tk start` or `tk close` of a message's step id now answers at once when the message repeated or called off one of the agent's open steps: it names that step and what became of it. A `tk start` or `tk close` of a step the chat closed for a later message is declined with that reason, and for a replaced step names the step that replaced it (`system/scripts/agent_message_steps.py`).

- The note beside a message sent while the agent was busy says the message may replace, repeat or call off an open step, and AGENTS.md describes those cases.
