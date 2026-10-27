- A `tk start` or `tk close` of a message's step id now answers at once when the message turned out to belong to one of the agent's open steps: it names that step, and says whether the chat left it, reworded it, or cancelled it. A `tk start` or `tk close` of a step a message cancelled is declined with that reason (`system/scripts/agent_message_steps.py`).

- The note beside a message sent while the agent was busy says the message may join, reword or cancel an open step instead of getting its own, and AGENTS.md describes those cases.
