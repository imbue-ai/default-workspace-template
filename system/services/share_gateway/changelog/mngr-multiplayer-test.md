- The grants parsers moved to the shared `share_grants` library; the gateway keeps only the agent key check (`agent_keys.py`).

- A visiting agent's last-seen time is refreshed every 10 seconds instead of every minute, so the shell's roster can show whether the agent is connected.
