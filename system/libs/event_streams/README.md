# event_streams

The archive rule for an append-only `events.jsonl` stream that a workspace
program writes forever: the shell's app announcements and the share gateway's
request counts. A stream that has grown past its threshold is moved aside
(`stage_oversized_stream`, a rename, so it is cheap enough to run before a
write) and later gzipped into an archive beside it
(`compress_staged_streams`, which also keeps only the newest archives), so the
next write starts a fresh file and the directory never grows without bound.

Archives are named `events.jsonl.<UTC timestamp>.gz`, a name mngr's event
replay never reads, and the stage step's `.archiving` suffix marks a stream a
program stopped between staging and compressing, which the next compress
finishes.

Stdlib-only, so the gateway (which imports nothing from mngr) and the shell
share it.
