# data/.secrets/

Per-secret env files. Two kinds live here:

- Files the minds app injects (for example `restic.env` for the backup repository
  and `share.env` for the sharing stack's materials).
- Files an agent asked the user for through the chat's secret card (the
  `connect-external-service` skill's `request_secret.py`): `<name>.env`, one line
  per variable as `NAME='value'`, written by the chat app with mode 0600. The
  agent never sees the value; it names the file and the variables, and the user
  types the value into the card.

Gitignored and never synced to GitHub; treat everything in here as sensitive.

## Reading a file

Only through the wrapper, which puts the file's variables into one process's
environment and nothing else:

```bash
python3 system/scripts/with_secrets.py data/.secrets/<name>.env -- <command...>
```

That form is what an agent's own commands, `.mcp.json` server commands,
supervisord programs, and scheduled jobs use. Nothing else may read the file:
a PreToolUse guard (`system/scripts/agent_secrets_guard.sh`) refuses `cat`,
`source`, `sed`, `python3 -c`, a redirect, or a `Read`/`Edit` tool call that
names the directory, because a value that lands in a tool call lands in the
transcript. `ls data/.secrets` and `rm data/.secrets/<name>.env` are allowed. To
rotate a value, request it again; the card merges the new value into the file.

The guard is a backstop against a slip, not a boundary: it judges the text of a
tool call, so it cannot see what a program run under the wrapper does with its
environment (the command after the wrapper's `--` is held to the same rule, so
it cannot be `cat`, but it can be a program that prints its own environment), and
a command that reaches the file by a path the checker does not recognise passes.
The rule that the value stays out of the transcript is the agent's to keep; the
guard catches the ordinary ways of breaking it.
