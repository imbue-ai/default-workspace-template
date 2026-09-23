# The direct API (row 4)

## What counts as an easy API

The service has an official SDK, a CLI, or a documented HTTP API, and it takes a
key the user can copy out of their account settings: an API key, a personal
access token, a webhook secret. You get the key through the secret card and
call the API from this workspace under the wrapper. Query-string keys, HMAC
signatures, and anything else latchkey cannot inject belong here too.

An API that needs an **OAuth app registration** (a client id and secret the user
creates in a developer console) is workable, but it asks the user to do
developer-console work. Offer it as the more technical option in one line and
default to the browser (row 5) unless they take it.

## The secret request

One tool call, nothing else in it, output untouched, then end the turn:

```bash
python3 .agents/skills/connect-external-service/scripts/request_secret.py \
  --file example --var EXAMPLE_API_KEY --rationale "I need your Example API key to look up the part numbers you asked about."
```

- `--file` names `data/.secrets/<file>.env`: a lowercase slug, one file per
  service. `--var` names each variable the card asks for (repeatable). The
  rationale is shown on the card as your claim, in the user's terms.
- The script prints the filed request as JSON: `request_id`, `file`,
  `variables`, `existing_variables` (what the file already holds),
  `overwrites` (the requested names that replace existing values), and the
  status. The chat renders a card with one password input per variable from
  that echo.
- **Stored**: a message `Secret stored: data/.secrets/example.env
  (EXAMPLE_API_KEY) (secret: stored, request_id: ...)` arrives. The file exists,
  mode 0600, with the named variables merged into whatever it held before.
- **Declined**: `Secret declined: ... (secret: declined, request_id: ...)` plus
  the user's note, if they left one. Move to the next row, or do what the note
  says.
- **Superseded**: filing a second request for the same file while one is pending
  closes the earlier card; only the newest accepts input.
- **Rotating a value** is a new request for the same file and variable; the
  card says which existing variables it replaces. **Removing** one is
  `rm data/.secrets/<file>.env`. You never edit the file.
- Where there is no chat app (the script says so and exits 1), the last resort
  is to ask the user to create the file from a terminal: one `NAME='value'` line
  per variable, then `chmod 600`.

## Running the SDK or CLI under the wrapper

The only way a value reaches a process:

```bash
python3 system/scripts/with_secrets.py data/.secrets/example.env -- example-cli projects list
python3 system/scripts/with_secrets.py data/.secrets/example.env -- uv run python scripts/pull_example.py
```

The wrapper loads the file's variables into the child's environment and execs the
command; the SDK reads `EXAMPLE_API_KEY` from `os.environ` like any other
setting. The same prefix goes into a supervisord program's `bash -c` (the
`build-app` skill's scaffold takes `--secrets-file`), a scheduled job's command,
and an MCP server command. A guard refuses `cat`, `source`, `sed`, `python3 -c`,
a redirect, or a `Read` of the file, because a value that lands in a tool call
lands in the transcript. The guard catches the slips it can recognise; it cannot
tell what the wrapped command does with its environment, so the rule is yours
to keep: never run `env`, a debugger, or anything that prints its environment
under the wrapper.

## The OAuth-app recipe

When the user takes the OAuth option:

1. Tell them, in their terms, to create an app in the service's developer
   console with the redirect URL you name, and to copy its client id and secret.
2. Request both through one card: `--file example --var EXAMPLE_CLIENT_ID --var
   EXAMPLE_CLIENT_SECRET`.
3. Run the SDK's authorization flow under the wrapper with its token cache
   pointed under `data/.secrets/` (for example `--token-file
   data/.secrets/example-token.json`, or the SDK's cache-directory setting), so
   the refreshed tokens live where the guard protects them.
4. The consent screen needs a browser whose `localhost` callback resolves inside
   this workspace: open the consent URL in a fleet browser (`agentic-browser-fleet
   new`, then `playwright-cli` to navigate) and `handoff` it to the user to
   approve.
5. Every later call runs under the wrapper; the SDK refreshes its own tokens from
   the cache.

## What to tell the user

Which service and what you will do with the access, that you are asking for its
key on a card in this chat, and that the key stays on this workspace's disk for
the programs that need it. Never the file path or the variable names, unless
they ask.
