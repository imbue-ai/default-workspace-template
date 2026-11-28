# GitHub access for a publish

The permissions a publish needs on the user's GitHub account, how to ask for
them, and how to confirm they are there. Every command runs from the repo
root, in a fresh shell, so each sets `PTM` at its start:

```bash
PTM=/home/user/workspace/.agents/skills/publish-template/python/publish_template_skill
```

It is where the GitHub access check lives, which this flow shares with
`publish-template`.

## Contents

- Somewhere to publish to
- Asking for the access
- Confirming the access

## Somewhere to publish to

On a first publish, ask before anything else:

> To publish, we'll put your app's code on your own account on GitHub. Do you
> have a GitHub account?

- **Yes:** file the permission requests now ("Asking for the access"), so the
  approval waits in Imbue Studio while you settle the scope.
- **No:** point them at <https://github.com/signup> (a free account is enough)
  and wait until they have one.
- **Another platform:** only GitHub is supported for apps. Say so.

On a later publish the account is known: probe the access, and file only
what is missing.

## Asking for the access

Probe first, and file only what it reports NOT permitted:

```bash
uv run --no-sync --project /home/user/workspace "$PTM/github_access.py" check
```

A kind reported UNKNOWN means the gateway could not answer (usually the
user's Imbue Studio app is closed): ask them to open it and check again
before filing anything.

There are two requests. Each must be the ONLY command in its tool call (the
chat builds the user's approval card from it):

```bash
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
    -H 'Content-Type: application/json' -H 'X-Latchkey-Device: *' \
    -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "predefined", "payload": {"scope": "github-rest-api", "permissions": ["github-read-user", "github-write-all"]}, "rationale": "Publish this app to a GitHub repository of its own on your account."}'
```

```bash
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
    -H 'Content-Type: application/json' -H 'X-Latchkey-Device: *' \
    -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "predefined", "payload": {"scope": "github-git", "permissions": ["github-git-write"]}, "rationale": "Push the published app to its repository."}'
```

Use exactly these permission names: creating a repository is
`POST /user/repos`, which the narrower `github-write-repos` does not cover.

## Confirming the access

Before the worker is dispatched, confirm the access is there:

```bash
uv run --no-sync --project /home/user/workspace "$PTM/github_access.py" check
```

When it is not there yet, wait for it in the background and end your turn:

```bash
python3 system/scripts/workspace_bare_scripts/run_in_background.py --description "Wait for the GitHub approval" --waiter -- \
    uv run --no-sync --project /home/user/workspace "$PTM/github_access.py" wait
```

Then, on a first publish, read the account:

```bash
latchkey curl -s https://api.github.com/user | python3 -c 'import json, sys; print(json.load(sys.stdin)["login"])'
```

That login is `owner`, the GitHub account the repository goes under. On a
later publish, `owner` and `repo_name` are the two path parts of the record's
`published_repo_url`.
