- Chats watch their files through one filesystem observer the app shares, so viewing a chat costs one watcher thread instead of about five, each holding up to ~1.9 MB of stack in a sandboxed workspace.

- The app answers as soon as it starts instead of after its first read of the agent list (about 40 s for 31 agents on a loaded workspace); a page connecting meanwhile gets the chat list when that read finishes.

- The idle-compaction sweep tells mngr which providers its chats are on, so mngr stops replaying its whole discovery event log every minute.
