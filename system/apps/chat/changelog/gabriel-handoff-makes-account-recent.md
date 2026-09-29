Switching a chat to an account on another provider now makes that account the most recently used one, as a switch within the same provider and a new chat already did. Before, the workspace's default stayed on the account the chat had just left. Workers, automations and every other `mngr create` that names no account kept launching there, even when it was the account the user had moved away from because it hit its usage limit.

Retrying a failed switch on a different account likewise makes that account the most recently used one.
