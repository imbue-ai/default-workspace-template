Spec the opencode transcript harness: a real session watcher/parser/tool-label table that
tails opencode's own `opencode.db` (SQLite, WAL) directly rather than the plugin's mirror
jsonl, replacing the current placeholder watcher. Design doc at
`docs/opencode_db_transcript_harness_spec.md`.
