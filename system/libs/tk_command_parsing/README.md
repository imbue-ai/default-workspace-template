# tk_command_parsing

Shell-aware parsing of `tk`/`ticket` command invocations, shared by the tooling
that drives the chat progress view.

`tk` lifecycle commands appear inside Bash commands that the progress view has
to reason about: which subcommand ran (`start` / `close` / `create` ...), with
which arguments, and whether the invocation was chained with or decorated by
other commands. Doing that with regexes is unreliable -- a `tk close` mentioned
inside a quoted summary, an operator inside a quoted string, an escaped quote
inside a `--step` title, or the `--flag=value` form all defeat a naive pattern.
This package tokenizes the command with `shlex` (a real shell-aware lexer)
instead, so quoting, escapes, comments, env-var prefixes, and operators are all
interpreted the way a shell would.

## API

- `parse_command(command) -> ParsedCommand | None` -- tokenize a Bash command
  and split it into `CommandSegment`s (one per command, divided at control
  operators). Each segment records its word tokens, whether a redirect
  decorates it, the control operator that ended it (`terminator`, `None` for
  the last segment -- what tells a harmless trailing `;` from a trailing `&`
  that backgrounds the command), and -- when it is a `tk`/`ticket` invocation --
  the subcommand verb and the tokens that follow it. Returns `None` when the
  command cannot be tokenized (e.g. unbalanced quotes).
- `extract_create_titles(command) -> list[str]` -- the titles created by
  `tk create --step "<title>"` invocations in a command, in order.
- `flag_values(args, flag) -> list[str]` -- the values passed to a flag within a
  token list, handling both the `--flag value` and `--flag=value` forms.

## Consumers

- `system/scripts/agent_tk_standalone_check.py` -- the PreToolUse gate that blocks a
  non-standalone `tk start`/`tk close`. It uses `parse_command` to split the
  Bash command into segments and inspect the verbs.
- `system/scripts/agent_latchkey_request_check.py` -- the PreToolUse gate that blocks a
  latchkey permission request that is batched, chained, or redirected. It reads
  each segment's words to find the ones that POST to the permission-requests
  host, and its `terminator` to allow a trailing `;` while still blocking a
  trailing `&`.

Both gates import this package under a bare `python3` (no virtualenv) via an
explicit `sys.path` entry, which is why this package is **stdlib-only** and must
stay that way.

`extract_create_titles` / `flag_values` are part of the parsing surface for
callers that need step titles out of a `tk create --step` command; keeping the
title-extraction logic here means any such caller reuses the same shell-aware
tokenizer rather than re-deriving it with a regex.
