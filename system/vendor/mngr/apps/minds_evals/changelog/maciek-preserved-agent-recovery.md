Background-worker evidence collection uses the public `mngr transcript --preserved` command for both
ATIF and JSONL capture, instead of reading the newest same-name archive directory itself. It targets
the listing's exact agent ID when the listing has one -- a launch records only a name, and a name a
destroyed worker released can be taken by another agent -- and otherwise resolves the launch name,
letting mngr reject an ambiguous preserved name rather than guessing by directory mtime. Each part
retries without `--preserved` when the flagged form fails, so a workspace whose pinned mngr predates
the flag still yields a live worker's document and stream.

A worker's identity and type come from its captured document when the listing did not name it. A
worker the capture could not identify at all is still embedded, built from its stream under a
launch-derived stand-in id, rather than dropped from the trajectory. The stand-in is only what the
evidence is filed under: the embedded worker's `extra.worker.agent_id` is empty, so nothing reports
an mngr agent id that was never resolved.

A worker absent from the agent listing is recorded as destroyed only when that listing was complete.
`mngr list` reports the agents it reached plus an `errors` array and a non-zero exit when a provider
is unreachable, and a worker missing from a listing like that is recorded as unknown.
