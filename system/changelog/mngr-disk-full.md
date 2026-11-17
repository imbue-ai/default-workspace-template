The shell's supervisord program runs its entry point directly; the shell now registers its own registry row (and keeps starting when the disk is too full to write it). `uv.lock` picks up the new `disk-headroom` dependency of `app-manifest` and the chat app.

A new built-in routine, "Clear deleted chats" (`system/routines/history-sweep.toml`), runs `history-sweep` every day at 04:00 to free the disk deleted chats leave behind; AGENTS.md now says a deleted chat stays recoverable for 30 days. Claude agents get `cleanupPeriodDays = 3650`, so claude no longer deletes session files older than 30 days, which are the history the chat app shows.

The mngr pin moves to a public export of the paired mngr branch, which logs `mngr observe`'s per-poll discovery lines at TRACE instead of DEBUG (about 94% less mngr file log on an idle workspace).
