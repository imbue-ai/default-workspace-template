---
name: publish-app
description: Publish one app this workspace built (or installed and then changed) to a public GitHub repository of its own on the user's account, as a version another workspace can install with install-app. Handles the first publish of an app and every later version. Use only when the user asks to publish, share publicly, or release an app or a new version of one. To share a whole workspace, use publish-template instead; to give particular people private access to an app, see the github-sync skill's dev repositories.
metadata:
  author: imbue
---

# Publish an app

A published app is a small public repository named after the app, with the
app's directory at its root: its code, the skills and routines it carries, its
`app.toml` and `README.md`. It holds one commit per published version, and it
is what `install-app` installs from. This skill makes the first version and
every later one.

A background worker prepares the version on a branch; you run the
conversation, the checks and the push. The steps that need no judgement are
subcommands of `uv run --no-sync app-install publish`, each ending in one JSON
line whose `result` says what happened (`system/libs/app_install/README.md`,
"Publishing an app").

## Three rules the whole flow rests on

1. **Only when the user asks.** Never publish, and never start this flow, on
   your own initiative: publishing makes the app public, for good.
2. **Two gates only the user can pass.** The scope gate (step 2) comes before
   anything is prepared, and the publish gate (step 5) before anything is
   pushed. Each needs an explicit reply to THAT message: the original "publish
   this", an earlier gate, and approving a GitHub permission do not count, and
   your own checks are verification, not confirmation. End your turn after
   sending each one.
3. **Nothing is public until step 6, and what goes is exactly what was
   scanned.** If any step before the push fails, nothing has left the
   workspace: fix the cause and run the step again, or stop and tell the user
   plainly that nothing was published. Never get a version out another way (a
   plain `git push`, files uploaded through the API, the app's dev
   repository made public): only `publish push` sends the tree the secret scan
   passed.

## Values carried through the flow

- `name` and `package`: the app's name and its directory under `system/apps/`
  (`uv run --no-sync app-install list` prints both).
- `repo_name`: the repository the app is published to. On a first publish it
  defaults to `name`; any name the user picks must match
  `^[A-Za-z0-9._-]+$` and not start with `-`. On a later publish it is the one
  in the record's `published_repo_url`, and is not asked again.
- `owner`: the GitHub account the repository is under (step 3).
- `version`: the version being published (step 2 proposes it).
- `slug`: `publish-` followed by the app's name (`publish-notes`). It names the
  worker, and the worker's branch is `mngr/<slug>`.
- `PTM=/home/user/workspace/.agents/skills/publish-template/python/publish_template_skill`:
  the GitHub access check this flow shares with `publish-template`.

Every command runs from the repo root of the live workspace
(`/home/user/workspace`), in a fresh shell: set the values a command uses at
its start. Note each value when you resolve it.

## Steps

```
- [ ] 0. Which app, and what state it is in
- [ ] 1. Somewhere to publish to (and start the GitHub approval)
- [ ] 2. The scope gate                             -- end turn, wait
- [ ] 3. Scan and record, in a background worker
- [ ] 4. Assemble and scan what will be published
- [ ] 5. The publish gate                           -- end turn, wait
- [ ] 6. Push
- [ ] 7. Record it, and close out
```

In the user's progress view these are a handful of plain-language steps
("Decide what to share", "Get the app ready (in the background)", "Publish
it"), not this list. Keep git and file words out of what you say
(`.agents/shared/references/user-facing-language.md`): the user is sharing an
app, and the one technical noun they need is the address of its page.

## 0. Which app, and what state it is in

```bash
uv run --no-sync app-install list
uv run --no-sync app-install publish compare <name>
```

`list` gives the app's `state`:

- `owned`: this workspace built it. Go on.
- `modified`: it was installed from someone else's repository (`upstream_url`)
  and changed here. It can be published, to a repository of the user's own:
  its first version records what it was derived from, and the scope gate says
  so.
- `installed`: someone else's app, unchanged. There is nothing of the user's
  to publish; give them the address it came from (`upstream_url`) to pass on
  instead, and stop.
- Not listed: it is not an app with a record (a built-in app, or one built
  before apps kept everything in their directory). Stop and say it cannot be
  published on its own; `update-self` converts an older app.

`compare` says what a publish would hold and what changed since the last one:
`is_published` and `published_version`, `changed_paths` (each `added`,
`modified` or `deleted`), `paths` (every file that would be published) and
`excluded_paths` (what the app's `[publish] exclude` already leaves out). It
reads the committed app: commit anything uncommitted in the app's directory
first.

## 1. Somewhere to publish to

On a first publish, ask before anything else:

> To publish, we'll put your app's code on your own account on GitHub, where
> anyone can see it. Do you have a GitHub account?

- **Yes:** file the two GitHub permission requests now, so the approval waits
  in Imbue Studio while you settle the scope. Each must be the ONLY command in
  its tool call (the chat builds the user's approval card from it):

  ```bash
  latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
      -H 'Content-Type: application/json' -H 'X-Latchkey-Device: *' \
      -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "predefined", "payload": {"scope": "github-rest-api", "permissions": ["github-read-user", "github-write-all"]}, "rationale": "Publish this app as a new public GitHub repository on your account."}'
  ```

  ```bash
  latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
      -H 'Content-Type: application/json' -H 'X-Latchkey-Device: *' \
      -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "predefined", "payload": {"scope": "github-git", "permissions": ["github-git-write"]}, "rationale": "Push the published app to its repository."}'
  ```

  Use exactly these permission names: creating a repository is
  `POST /user/repos`, which the narrower `github-write-repos` does not cover.
  Probe first with `uv run --no-sync --project /home/user/workspace "$PTM/github_access.py" check`
  and file only what it reports NOT permitted; a kind reported UNKNOWN means
  the gateway could not answer (usually the user's Imbue Studio app is
  closed): ask them to open it and check again before filing anything.
- **No:** point them at <https://github.com/signup> (a free account is enough)
  and wait until they have one.
- **Another platform:** only GitHub is supported for apps. Say so.

On a later publish the account is known; probe the access and file only what
is missing.

## 2. The scope gate

Work out the proposal first:

- **The version.** A first publish is `0.1.0` unless the user says otherwise.
  After that, read `changed_paths` and the changes themselves
  (`git diff <published_commit> <tree>`, both from `compare`'s result) and
  propose the bump: a patch for fixes, a minor version for something new, and
  a **major** version when whoever installed the app would have to act
  because its stored data changed shape or it needs a new secret or
  permission.
- **What to leave out.** Anything in the app's directory the user would not
  want public: internal notes, drafts, sample data about real people. Each
  becomes a glob in the manifest's `[publish] exclude`.

Then send ONE message saying, in plain language:

- what WILL be published: the app, with the skills and automations it carries,
  described as features and not as a file list;
- what will NOT be: the app's stored data, the user's other apps and
  everything else in the workspace, secret values such as keys and passwords,
  and anything you propose to leave out (name each);
- the proposed version, and for a major one what people who installed the app
  will have to do;
- the repository's name, marked as adjustable on a first publish;
- that the repository will be **public** and will ship under the MIT license;
- for a `modified` app: that this is their version of an app that came from
  `<upstream_url>`, published under their own account with a note of where it
  came from.

Then end your turn and wait for their reply.

## 3. Scan and record, in a background worker

Only after the user replied to the scope gate.

Confirm the GitHub access, waiting in the background and ending your turn if
it is not there yet, then read the account on a first publish:

```bash
uv run --no-sync --project /home/user/workspace "$PTM/github_access.py" check
```

```bash
python3 system/scripts/workspace_bare_scripts/run_in_background.py --description "Wait for the GitHub approval" --waiter -- \
    uv run --no-sync --project /home/user/workspace "$PTM/github_access.py" wait
```

```bash
latchkey curl -s https://api.github.com/user | python3 -c 'import json, sys; print(json.load(sys.stdin)["login"])'
```

That login is `owner`. On a later publish, `owner` and `repo_name` are the two
path parts of the record's `published_repo_url`.

Dispatch the worker. Per `launch-task`, the delegation is one step in your
timeline ("Get the app ready (in the background)"). If an earlier attempt left
a worker or branch named `<slug>`, remove it first
(`create_worker.py destroy --name <slug>`, then
`git branch -D mngr/<slug>`). The task file holds parameters only; the
instructions are in `references/scan-worker.md`:

````bash
mkdir -p data/.tasks/launch-task/<slug>
cat > data/.tasks/launch-task/<slug>/task.md << 'TASK_EOF'
---
finish_report_path: data/.tasks/launch-task/<slug>/reports/report.md
---

# Task: Get the "<name>" app ready to publish

Read `.agents/skills/publish-app/references/scan-worker.md` in your checkout
and follow it.

- app: <name>
- package: <package>
- version: <version>
- last published: <published_version, or "never: this is the first publish">
- repository: https://github.com/<owner>/<repo_name>
- leave out of what is published: <one glob per line, relative to the app's directory, or "nothing more">
- derived from: <upstream_url, for a modified app, or "nothing">
- context: <what the app does for its user, the accounts and services it
  depends on, known gaps -- what the worker needs to write good guidance for
  whoever installs it, and a good README>
TASK_EOF
````

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py launch \
    --name <slug> --template worker \
    --runtime-dir data/.tasks/launch-task/<slug>/ \
    --task-file data/.tasks/launch-task/<slug>/task.md
```

```bash
python3 system/scripts/workspace_bare_scripts/run_in_background.py --description "Wait for the background agent" --waiter -- \
    uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py await \
    --name <slug> --task-file data/.tasks/launch-task/<slug>/task.md
```

Handle `question` gates and timeouts per
`.agents/shared/references/lead-proxy.md`. On `done`, do NOT merge yet and do
not destroy the worker: its branch is merged in step 7, once the version is
published, so that a publish the user calls off leaves the workspace's copy of
the app as it was. The report names the version it settled on (it raises the
version to a major one when it finds that whoever installed the app must act)
and anything it could not determine.

## 4. Assemble and scan what will be published

```bash
uv run --no-sync app-install publish assemble <name> --commit mngr/<slug>
```

It builds what would be published (the app's directory at the worker's commit,
less what `[publish] exclude` names) in a directory outside the workspace,
runs the secret scan over it, and for a Python app checks that the app locks
with no workspace around it. Nothing is pushed. Act on `result`:

- `assembled`: go on. Keep `version`, `paths`, `excluded_paths` and `needs`
  for the publish gate.
- `secrets_found`: `findings` names each file, never the value. Nothing was
  published. Have the worker remove the value from the app (a secret belongs
  in `data/.secrets/`, declared under `[[secrets]]`) or, when the file should
  not be public at all, add it to `[publish] exclude`; then run this step
  again. Tell the user in one plain sentence what was found and that it is
  being taken out. If the value was ever saved in the app's history, tell
  them it is still in the app's private dev repository, when it has one, and
  that a real key should be replaced with a new one.
- `lock_check_failed`: the app's dependencies do not resolve on their own.
  Have the worker make the lock again in the app's directory (`uv lock`) and
  commit it. If it still fails, the app depends on something that exists only
  inside a workspace (the usual case is a `path = "../../libs/..."` entry
  under `[tool.uv.sources]` in its `pyproject.toml`, which resolves nowhere
  else): stop, and tell the user the app cannot be published until that is
  changed. Nothing was published.
- `refused`: `detail` says why (no version in the manifest, a version not
  newer than the last published, a manifest that fails its check). Have the
  worker fix it.

Send a fix to the worker as a message (per `lead-proxy.md`) rather than making
it yourself, wait for its next report, and assemble again: only the commit you
assemble is what gets pushed.

## 5. The publish gate

Present the publish ONCE, in plain language:

- the app's **name** and the **version**;
- **what it contains**, from `paths`, as features ("the app itself, the weekly
  digest it runs, the skill for adding sources"), and **what was left out**,
  from `excluded_paths`;
- **what whoever installs it will be asked for**, from `needs`: the
  permissions, the secret values (what each is and where to get it), whether
  it uses an AI model, anything it installs on their workspace, other apps it
  expects, and what they will have to decide or change. Ask: "does that look
  right and complete?" A gap here silently breaks the app for whoever
  installs it;
- the **README**, pasted as your message so it renders as a page
  (`git show mngr/<slug>:system/apps/<package>/README.md`), asking
  whether it reads like a good description of what they built. Say up front
  that the button at its top shows as a plain link in chat;
- the **address** it will have, `https://github.com/<owner>/<repo_name>`, and
  once more that this makes it **public**, under the MIT license.

Then end your turn and wait for their explicit go-ahead to THIS message.

Changes they ask for go to the worker, followed by step 4 again, and the
changed part shown again. If they call it off, stop here: destroy the worker
and its branch (step 7's commands, without the merge), and say nothing was
published.

## 6. Push

```bash
uv run --no-sync app-install publish push <name> --repo-name <repo_name>
```

(`--repo-name` matters only on a first publish, and only when it is not the
app's name.) On a first publish this creates the public repository, closes its
issues, wiki, projects, discussions and outside pull requests, and gives it
the topic `imbue-studio-app`. It then pushes the assembled tree as ONE commit
on top of the last published one, and the tag `v<version>`. It never forces
anything. Act on `result`:

- `pushed`: the version is public. Keep `published_repo_url`. Check
  `is_tag_pushed` before going on. When it is false the version has no tag,
  and whoever installs or updates the app is given the highest tag, so they
  would keep getting the version before this one: run this step again now (it
  sends only the tag), because step 7 ends the publish and nothing sends the
  tag after it. If the tag still does not go, or `problems` says the
  repository already has that tag (someone made it on GitHub, and it is never
  moved), stop and tell the user, and run this step and step 7 once it is
  sorted out. Every other entry of `problems` is a follow-up to mention (a
  setting that did not take), not a failed publish. Then go to step 7.
- `name_taken`: the account already has a repository of that name. Nothing
  was published. Go back to the scope gate for the name alone: agree another
  with the user, have the worker point the README's links at it, run step 4
  again, show the README again, and push with the new `--repo-name`.
- `push_refused`: the published repository holds changes this workspace did
  not make (someone edited it on GitHub, or another workspace publishes the
  same app). Nothing was pushed. Stop and tell the user; never try to
  overwrite it.
- `not_assembled`: run step 4 first.
- `refused`: `detail` says why, with what to do when the cause is known (the
  push permission is missing: request it again as in step 1, then run this
  again). Run it again once the cause is fixed: a repository that was created
  is not created twice, and a version that reached the repository is not
  pushed twice.

## 7. Record it, and close out

Merge the worker's branch as `lead-proxy.md` describes for a `done` report
(`git merge --no-ff mngr/<slug>`), then:

```bash
uv run --no-sync app-install publish record <name>
```

It writes the repository, the published commit and the version into the app's
record and saves that one change. If this step is interrupted, or its
`problems` say the record was not saved, the app IS published: run this
command again, and never push again. `not_assembled` after a run that
answered `recorded` means there is nothing left to record
(`app-install list` shows the app's `published_version`).

Then destroy the worker and its branch:

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py destroy --name <slug>
git worktree prune
git branch -d "mngr/<slug>"
```

If you are stopping after a failure instead, leave the worker and its branch
for a retry.

Give the user the app's address (`published_repo_url`) in your final message,
and the two ways someone can take it: the "Open in Imbue Studio" button on
that page, or pasting the address to their own workspace's chat. Mention any
follow-up from step 6.

## Things to know

- **A later version is this same skill.** Step 0's `compare` shows what
  changed; the repository and its name are already settled.
- **Changes do not flow back.** The published repository is an export. An
  edit made on GitHub is not brought into the workspace, and the next publish
  is refused rather than overwriting it (`push_refused`).
- **Sharing with particular people without publishing** is the app's private
  dev repository (the `github-sync` skill). It is unfiltered and has had no
  secret scan, so it is never what this skill makes public.
- **Unpublishing** is the user's to do on GitHub. `app-install uninstall`
  leaves an app's repositories alone.
