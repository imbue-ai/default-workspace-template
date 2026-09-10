The `file-sharing` skill now describes the folders a user keeps synced with the machine, which agents could previously find in their home directory with nothing explaining them.

`~/synced_folders/<device id>/<the folder's path on the user's computer>` is a folder that is syncing now. The files are ordinary and writable, and changes go back to the user's computer.

`~/inactive_synced_folders/...` holds a copy whose syncing the user has turned off, and the skill tells agents to leave it alone. Minds moves folders in and out of that directory by name and clears the destination first, so an agent's own files sitting where a folder belongs are deleted the next time the user turns syncing off. Agents are pointed at their working directory or `/tmp` instead.
