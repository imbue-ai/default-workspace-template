---
name: publish-app
description: Publish one app this workspace built (or installed and then changed) to a GitHub repository of its own on the user's account, private unless the user wants it public, as a version another workspace can install with install-app. Handles the first publish of an app, every later version, and making a published app public. Use only when the user asks to publish, share, release or put on GitHub an app or a new version of one. To share a whole workspace, use publish-template instead.
metadata:
  author: imbue
---

# Publish an app

A published app is a small repository named after the app, private unless the
user wants it public, with the app's directory at its root: its code, the
skills and routines it carries, its `app.toml` and `README.md`. It holds one
commit per published version, and it is what `install-app` installs from.
This skill makes the first version and every later one.

A background worker prepares the version on a branch; you run the
conversation, the checks and the push. Each step that needs no judgement is a
`uv run --no-sync app-install publish` subcommand ending in one JSON line
with a `result` (`system/libs/app_install/README.md`, "Publishing an app").

## Standing instructions

- **Only when the user asks.** Never publish, and never start this flow, on
  your own initiative: publishing copies the app out of the workspace.
- **Private unless the user asked for public.** "Publish it", "put it on
  GitHub", "share it with my team" make a private repository: only the user
  and the people they invite on GitHub can see or install it. Words that mean
  everyone ("publicly", "open source", "so anyone can install it") make a
  public one, for good. When you cannot tell which they mean, ask at the scope
  gate. Never make anything public on a guess.
- **One gate only the user can pass.** The scope gate (step 3) comes before
  anything is prepared, and their yes to it is the go-ahead to publish what
  it described. It needs an explicit reply to THAT message: the original
  "publish this" and approving a GitHub permission do not count. Ask again
  only when what was prepared differs from what they approved (step 6).
- **"Sync" or "back up" is not "publish".** Those words may mean the app's
  dev repository, which follows the app by itself (the `github-sync` skill).
  Ask which they mean before step 2: a version people can install, or a
  private copy that stays current.
- **Nothing leaves the workspace until step 7, and what goes is exactly what
  was scanned.** If any step before the push fails, nothing has left the
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
  Their progress view shows a few plain steps ("Decide what to share", "Get
  the app ready (in the background)", "Publish it"), not the list below.

## Other tasks

- **Changes do not flow back.** The published repository is an export: an
  edit made on GitHub is not brought into the workspace, and the next publish
  is refused rather than overwriting it.
- **Sharing with particular people** is a private publish: the user then
  invites them to the repository on GitHub. Never the app's dev repository
  (the `github-sync` skill), which has had no secret scan.
- **Making a published app public later**, only when the user asks for
  exactly that: read "Making a published app public" in
  `references/results.md`.
- **Making it private again, or unpublishing**, is the user's to do on
  GitHub. `app-install uninstall` leaves an app's repositories alone.

## Steps

1. Find the app and the state it is in.
2. Get somewhere to publish to. *Ends the turn* while the user has no GitHub
   account.
3. The scope gate. *Ends the turn:* wait for the user's reply.
4. Get the version ready, in a background worker.
5. Assemble and scan what will be published.
6. Check it is what the user approved. *Ends the turn* only when it differs.
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
is ALREADY ON GITHUB.

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
  want shared: internal notes, drafts, sample data about real people. Each
  becomes a glob in the manifest's `[publish] exclude`.

Then send ONE message saying, in plain language (with what "At the scope
gate" in `references/skills-the-app-needs.md` adds, when step 1 sent you
there):

- what WILL be published: the app, with the skills and automations it carries,
  described as features and not as a file list;
- what will NOT be: the app's stored data, the user's other apps and
  everything else in the workspace, secret values such as keys and passwords,
  and anything you propose to leave out (name each);
- the proposed version, and for a major one what people who installed the app
  will have to do;
- the repository's name, marked as adjustable on a first publish;
- who will be able to see it. **Private**: only they and the people they
  invite on GitHub. **Public**, only when they asked for that: anyone, for
  good, under the MIT license. When their request did not say which, ask here
  and offer private as the default. On a later publish (`is_published` in
  step 1) say which it is now, and never ask
  (`uv run --no-sync app-install publish visibility <name>`; when
  `is_private` is `null`, say its GitHub page shows it);
- for a `modified` app: that this is their version of an app that came from
  `<upstream_url>`, published under their own account with a note of where it
  came from;
- that on their yes you will get it ready and publish it, with no further
  question unless something turns out different from this.

Then end your turn and wait for their reply.

## Step 4: Get the version ready, in a background worker

Only after the user replied to the scope gate.

1. Confirm the GitHub access and read the account the repository goes under
   (`owner`): "Confirming the access" in `references/github-access.md`. End
   your turn if the approval is not there yet.
2. Read `references/dispatching-the-worker.md` and dispatch the worker with
   the task file and the commands it gives. The worker is `<slug>`,
   `publish-` followed by the app's name (`publish-notes`), and its branch is
   `mngr/<slug>`. Its own instructions are `references/scan-worker.md`, a
   guide written for it and not for you.
3. Lead it as the `launch-task` skill says a lead does: its `question` gates,
   a timeout, a message to it.

On `done`, the report names the version the worker settled on (it raises the
version to a major one when it finds that whoever installed the app must act)
and anything it could not determine. Do not merge or destroy the worker yet.
On `stuck`, nothing was published: read "When the worker is stuck" in
`references/results.md`.

## Step 5: Assemble and scan what will be published

```bash
uv run --no-sync app-install publish assemble <name> --commit mngr/<slug>
```

It builds what would be published (the app's directory at the worker's commit,
less what `[publish] exclude` names) outside the workspace, runs the secret
scan over it, and checks that the app stands alone there. Nothing is pushed.

On `assembled`, go on: keep `version`, `paths`, `excluded_paths` and `needs`
for step 6 and your final message. On any other `result` (`secrets_found`,
`outside_skills_found`, `lock_check_failed`, `refused`), read "When
assembling stops" in `references/results.md`, have the worker fix what it
names, wait for its next report, and assemble again.

## Step 6: Check it is what the user approved

The user's yes at the scope gate covers what that message described. Go
straight on to step 7 unless one of these holds:

- the worker settled on **another version** than the one agreed;
- the app **took in a skill**, or leaves something out, that the scope gate
  did not name;
- the worker's report names something it **could not determine**, or
  something personal it left in and only noted;
- `needs` asks whoever installs the app for something the user would not
  expect (a permission, a secret value).

Only then: say in ONE message what differs and what it means, end your turn,
and wait for their go-ahead to it. Changes they ask for go to the worker,
then step 5 again. If they call it off, follow "When the user calls it off"
in `references/results.md`, and say nothing was published.

## Step 7: Push

```bash
uv run --no-sync app-install publish push <name> --repo-name <repo_name> [--public]
```

(`--repo-name` only on a first publish under another name.) A first publish
creates the repository, private unless `--public` is given: pass it only when
the user chose public at the scope gate. A later publish never changes who
can see the repository: leave `--public` off. The assembled tree is then
pushed as ONE commit on top of the last published one, with the tag
`v<version>`, never forced.

On `pushed` the version is on GitHub: keep `published_repo_url`, and check
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
`references/results.md`. Then read `references/after-publishing.md` and do
what it says, before you tell the user anything.

Then destroy the worker and its branch:

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py destroy --name <slug>
git worktree prune
git branch -d "mngr/<slug>"
rm -rf data/.tasks/launch-task/<slug>
```

If you are stopping after a failure, leave the worker and its branch for a
retry.

In your final message say what went up: the version, what whoever installs
it will be asked for (`needs`), and the app's address (`published_repo_url`),
where its description page is. Give the two ways to take the app: the "Open
in Imbue Studio" button on that page, or pasting the address into a
workspace's chat. For a private one, say that only people they invite to it
on GitHub can do either. Mention any follow-up from step 7, and that a
change they want goes out as the next version.
