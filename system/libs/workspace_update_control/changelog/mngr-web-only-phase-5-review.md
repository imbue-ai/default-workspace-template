The workspace says what its update is doing, so a client no longer works that out for itself.

- `state` is new. `read_update_state` answers one of seven activities (`IDLE`, `STARTING`, `RUNNING`, `WAITING`, `APPLYING`, `FINISHING`, `STALLED`), the run with what the user dismissed of it left out, a start that made no chat, the armed schedule with the window it may next start in, whether a sign-in is needed, whether the dispatcher's next minute arms the update by itself, and what a client may ask next. It is worked out when asked, from the workspace's files, a listing of its agents and its clock, with the dispatcher's own decisions, and writes nothing. It replaces `control.read_update_state`, which answered the files as they were.

- A run reads as waiting at once for a hold its lead recorded, and for an idle lead once its last turn ended forty seconds ago. `agents` reads when each agent's last turn ended from the observer's events; where a harness records none, a minute of the watchdog that found the run idle stands in.

- The chats waiting on background tasks are read only for a lead idle long enough to read as waiting, and whether an account is signed in only while a refusal for want of one is on record.

- A start or an arm that finds no AI account signed in leaves `sign-in-refusal.json`, which the next one that finds an account takes away. A start records the version the user named for it in `last-start.json`, and `dismiss_start_failure` records there that the user put a failed start away.

- `dispatcher` gains `unrecorded_dispatch`, `next_window` and `watchdog_idle_ticks`, and `arming` gains `armable_target`, which the dispatcher's minute and the read share.

No file a released desktop reads or writes changes format.
