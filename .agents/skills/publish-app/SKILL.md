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
conversation, the checks and the push. Each step that needs no judgement is a
`uv run --no-sync app-install publish` subcommand ending in one JSON line
with a `result` (`system/libs/app_install/README.md`, "Publishing an app").

## Standing instructions

- **Only when the user asks.** Never publish, and never start this flow, on
  your own initiative: publishing makes the app public, for good.
- **Two gates only the user can pass.** The scope gate (step 3) comes before
  anything is prepared, and the publish gate (step 6) before anything is
  pushed. Each needs an explicit reply to THAT message: the original "publish
  this", an earlier gate, and approving a GitHub permission do not count, and
  your own checks are verification, not confirmation. End your turn after
  sending each one.
- **Nothing is public until step 7, and what goes is exactly what was
  scanned.** If any step before the push fails, nothing has left the
  workspace: fix the cause and run the step again, or stop and tell the user
  plainly that nothing was published. Never get a version out another way (a
  plain `git push`, files uploaded through the API, the app's dev repository
  made public): only `publish push` sends the tree the secret scan passed.
- **Never merge the worker's branch before step 8.** It is merged once the
  version is published, so that a publish the user calls off leaves the
  workspace's copy of the app as it was. A fix goes to the worker as a
  message, never into the branch by your own hand: only the commit you
  assemble is what gets pushed.
- **Run every command from the repo root of the live workspace**
  (`/home/user/workspace`), in a fresh shell: set the values a command uses at
  its start, and note each value when you resolve it.
- **Keep git and file words out of what you say**
  (`.agents/shared/references/user-facing-language.md`): the user is sharing
  an app, and the one technical noun they need is the address of its page.
  Their progress view shows a few plain steps ("Decide what to share",
  "Publish it"), not the list below.

## Other tasks

- **Changes do not flow back.** The published repository is an export: an
  edit made on GitHub is not brought into the workspace, and the next publish
  is refused rather than overwriting it.
- **Sharing with particular people without publishing** is the app's private
  dev repository (the `github-sync` skill), which has had no secret scan and
  is never what this skill makes public.
- **Unpublishing** is the user's to do on GitHub. `app-install uninstall`
  leaves an app's repositories alone.

## Steps

1. Find the app and the state it is in.
2. Get somewhere to publish to. *Ends the turn* while the user has no GitHub
   account.
3. The scope gate. *Ends the turn:* wait for the user's reply.
4. Get the version ready, in a background worker.
5. Assemble and scan what will be published.
6. The publish gate. *Ends the turn:* wait for the user's go-ahead.
7. Push.
8. Record it, and close out.

## Step 1: Find the app and the state it is in

```bash
uv run --no-sync app-install list
uv run --no-sync app-install publish compare <name>
```

`list` prints the app's `name` and its `package` (its directory under
`system/apps/`), which every later step uses, and its `state`:

- `owned`: this workspace built it. Go on.
- `modified`: it was installed from someone else's repository (`upstream_url`)
  and changed here. It is published to a repository of the user's own, and
  its first version records what it was derived from.
- `installed`, or not listed: stop. "An app that cannot be published" in
  `references/results.md` says what to tell the user.

`compare` says what a publish would hold (`paths`, `excluded_paths`), what
changed since the last one (`is_published`, `published_version`,
`changed_paths`), and each line that names a skill the app does not carry
(`outside_skill_uses`). It reads the committed app: commit anything
uncommitted in the app's directory first.

When `outside_skill_uses` is not empty, `is_outside_skills_checked` is
false, the app's record lists a skill under `[[references]]`, or one of its
routines (`routines/*.toml`) has a `skill`, read
`references/skills-the-app-needs.md` before step 3: the app takes such a
skill in, which changes steps 3, 4 and 8.

Then look for a publish an earlier attempt left between its steps:
`cat data/.state/app_install/publish/<name>.json`. No such file means nothing
is waiting. When there is one, read "A publish that was pushed and never
finished" in `references/results.md` before anything else: a pushed version
is ALREADY PUBLIC.

## Step 2: Get somewhere to publish to

Follow "Somewhere to publish to" in `references/github-access.md`: a first
publish starts by asking the user whether they have a GitHub account, and
files the permission requests so the approval waits while you settle the
scope.

## Step 3: The scope gate

Work out the proposal first:

- **The version** (`version`). A first publish is `0.1.0` unless the user
  says otherwise. After that, read `changed_paths` and the changes themselves
  (`git diff <published_commit> <tree>`, both from `compare`'s result) and
  propose the bump: a patch for fixes, a minor version for something new, and
  a **major** version when whoever installed the app would have to act
  because its stored data changed shape or it needs a new secret or
  permission.
- **The repository's name** (`repo_name`). On a first publish it defaults to
  the app's name; any name the user picks must match `^[A-Za-z0-9._-]+$` and
  not start with `-`. On a later publish it is the one in the record's
  `published_repo_url`, and is not asked again.
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

## Step 4: Get the version ready, in a background worker

Only after the user replied to the scope gate.

1. Confirm the GitHub access and read the account the repository goes under
   (`owner`): "Confirming the access" in `references/github-access.md`. End
   your turn if the approval is not there yet.
2. Dispatch the worker with the commands below, as `<slug>`: `publish-`
   followed by the app's name (`publish-notes`). Its branch is `mngr/<slug>`.
   Remove one an earlier attempt left first ("A worker an earlier attempt
   left" in `references/results.md`).
3. Lead it as the `launch-task` skill says a lead does: its `question` gates,
   a timeout, a message to it.

The task file holds parameters only; the worker's instructions are in
`references/scan-worker.md`, a guide written for it and not for you:

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
- skills to take in: <each skill, then the paths that also use it, or "none">
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

On `done`, the report names the version the worker settled on (it raises the
version to a major one when it finds that whoever installed the app must act)
and anything it could not determine. Do not merge or destroy the worker yet.

## Step 5: Assemble and scan what will be published

```bash
uv run --no-sync app-install publish assemble <name> --commit mngr/<slug>
```

It builds what would be published (the app's directory at the worker's commit,
less what `[publish] exclude` names) outside the workspace, runs the secret
scan over it, and checks that the app stands alone there. Nothing is pushed.

On `assembled`, go on: keep `version`, `paths`, `excluded_paths` and `needs`
for the publish gate. On any other `result` (`secrets_found`,
`outside_skills_found`, `lock_check_failed`, `refused`), read "When
assembling stops" in `references/results.md`, have the worker fix what it
names, wait for its next report, and assemble again.

## Step 6: The publish gate

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

Changes they ask for go to the worker, followed by step 5 again, and the
changed part shown again. If they call it off, stop here: follow "When the
user calls it off" in `references/results.md`, and say nothing was published.

## Step 7: Push

```bash
uv run --no-sync app-install publish push <name> --repo-name <repo_name>
```

(`--repo-name` only on a first publish under another name.) A first publish
creates the public repository. The assembled tree is then pushed as ONE
commit on top of the last published one, with the tag `v<version>`, never
forced.

On `pushed` the version is public: keep `published_repo_url`, and check
`is_tag_pushed` and `problems` before step 8, as "After a push" in
`references/results.md` says. On any other `result`
(`name_taken`, `push_refused`, `not_assembled`, `refused`), read "When the
push stops" there.

## Step 8: Record it, and close out

Merge the worker's branch (`git merge --no-ff mngr/<slug>`), then:

```bash
uv run --no-sync app-install publish record <name>
```

It writes what was published into the app's record. If this step is
interrupted, or its result is not `recorded` with no `problems`, the app IS
published: never push again, and read "When recording stops" in
`references/results.md`. When the app took in a skill, finish with "After
the version is published" in `references/skills-the-app-needs.md`.

Then destroy the worker and its branch:

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py destroy --name <slug>
git worktree prune
git branch -d "mngr/<slug>"
```

If you are stopping after a failure instead, leave the worker and its branch
for a retry.

In your final message give the user the app's address (`published_repo_url`)
and the two ways to take it: the "Open in Imbue Studio" button on that page,
or pasting the address into a workspace's chat. Mention any follow-up from
step 7.
