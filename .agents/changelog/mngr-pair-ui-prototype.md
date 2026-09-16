The `file-sharing` skill now describes the folders a user keeps synced with the machine, which agents could previously find in their home directory with nothing explaining them.

Agents are told to prefer a synced copy over the WebDAV file server when there is one: it is ordinary local files, so their normal tools work, there is no round trip per file, and it keeps working while the user's computer is asleep or offline.

`~/synced_folders/<device id>/<the folder's path on the user's computer>` is a folder that is syncing now. Whether an agent's writes reach the user depends on the access it was granted, and the skill says how to check: with read and write, changes travel both ways; with read only, the sync is one-way and anything the agent writes there is reverted and anything it creates is deleted, silently, the next time the sync runs. An agent that treated a read-only synced folder as a place to put its work would lose it.

It also says what does not come across -- `.git`, so a synced repository arrives as a working tree with no history and `git init` is the wrong response; and symlinks, in either direction -- and that deleting a file in a synced folder deletes the user's copy of it, with no undo.

`~/inactive_synced_folders/...` holds a copy whose syncing the user has turned off, and the skill tells agents to leave it alone. Minds moves folders in and out of that directory by name and clears the destination first, so an agent's own files sitting where a folder belongs are deleted the next time the user turns syncing off. Agents are pointed at their working directory or `/tmp` instead.
