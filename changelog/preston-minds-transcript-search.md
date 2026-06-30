- Added a `find-past-transcripts` skill: when an agent that ran on this workspace
  host is destroyed (e.g. a sub-agent launched via `launch-task`, or an earlier
  session), mngr preserves its conversation transcript locally on this host under
  `/mngr/preserved/`. The skill finds and reads those (with `find`/`cat`/`jq`),
  so an agent can recover the chat history of earlier agents on the same host.

- Added a "Finding past work" note to `CLAUDE.md` so agents know by default that
  earlier agents' transcripts on this host are preserved and where to find them.
