# When a publish step does not go straight through

What to do when a step of a publish meets something other than the expected
result: an app that cannot be published, what an earlier attempt left behind,
a worker that reports it is stuck, each other `result` of
`app-install publish assemble`, `push` and `record`, and a publish the user
calls off. Step numbers are SKILL.md's.

## Contents

- An app that cannot be published
- A publish that was pushed and never finished
- A worker an earlier attempt left
- When the worker is stuck
- When assembling stops
- When the user calls it off
- After a push
- When the push stops
- When recording stops
- Making a published app public

## An app that cannot be published

- `installed` in `app-install list`: someone else's app, unchanged. There is
  nothing of the user's to publish; give them the address it came from
  (`upstream_url`) to pass on instead.
- Not listed: it is not an app with a record (a built-in app, or one built
  before apps kept everything in their directory). Say it cannot be published
  on its own; `update-self` converts an older app.

## A publish that was pushed and never finished

`data/.state/app_install/publish/<name>.json` is what an earlier attempt left
between its steps.

- `is_commit_pushed` is `true`: that version is ALREADY ON GITHUB and only its
  record (and perhaps its tag) is missing. Do not start over, and do not
  remove the worker or its branch `mngr/<slug>`: that branch holds the
  published version's own changes to the app, and step 8 merges it. Go
  straight to step 7, which sends only what is still missing, then step 8.
- `is_commit_pushed` is `false`: a tree that was assembled and never pushed.
  Nothing has left the workspace, and starting over replaces it.

## A worker an earlier attempt left

If an earlier attempt left a worker or branch named `<slug>`, remove it
before dispatching a new one:

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py destroy --name <slug>
git branch -D mngr/<slug>
```

Never do that for a version that was pushed and not recorded (the section
above): you are not at the dispatch step at all.

## When the worker is stuck

A `stuck` report quotes what stopped the worker. Nothing was published, and
the workspace's copy of the app is as it was: the worker's branch is not
merged.

- **The app needs an app SDK that is not released yet.** An app built here
  runs on the workspace's own app SDK, and a published one has to run on a
  released one. The worker found none the app's tests pass on. The app works
  and cannot be shared yet. Tell the user that in their terms (it is built on
  a part of Imbue Studio that is newer than the last release, so sharing it
  has to wait for the next one), and that nothing was published.
- **The app cannot take in a skill it needs**, or cannot do without something
  else that exists only in this workspace (the report names it). Decide with
  the user whether the app is worth publishing without it ("Which skills" in
  `skills-the-app-needs.md`). If it is, tell the worker to leave it out and
  to take out of the app whatever needs it, and wait for its next report; if
  not, the app cannot be published as it is.
- **Anything else.** When the report shows a fix you can describe, send it to
  the worker as a message and wait for its next report. Otherwise tell the
  user plainly that nothing was published, and why.

When the publish stops at one of the first two, remove the worker as "When
the user calls it off" below does. When it stops at anything else, stop the
worker and leave it and its branch where they are, so that what went wrong
can still be read; the next attempt removes them ("A worker an earlier
attempt left" above):

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py stop --name <slug>
```

## When assembling stops

Nothing was published. Send the fix to the worker as a message, never make it
yourself, wait for its next report, and assemble again: only the commit you
assemble is what gets pushed.

- `secrets_found`: `findings` names each file, never the value. Have the
  worker remove the value from the app (a secret belongs in `data/.secrets/`,
  declared under `[[secrets]]`) or, when the file should not be published
  at all, add it to `[publish] exclude`; then assemble again. Tell the user in
  one plain sentence what was found and that it is being taken out. If the
  value was ever saved in the app's history, tell them it is still in the
  app's private dev repository, when it has one, and that a real key should
  be replaced with a new one.
- `outside_skills_found`: `uses` names each line of the app that still names
  a skill of this workspace the app does not carry
  (`.agents/skills/<skill>/...`, `data/.skills/<skill>/...`). Read "Which
  skills" and "A skill something else uses too" in
  `skills-the-app-needs.md`, decide what becomes of the skill, and have the
  worker take it in or take the line out. A skill the app takes in changes
  what the user agreed to at the scope gate, so tell them before the publish
  gate.
- `lock_check_failed`: the app's dependencies do not resolve on their own.
  Have the worker make the lock again in the app's directory (`uv lock`) and
  commit it. If it still fails, the app depends on something that exists only
  inside a workspace: a skill's package (have the worker take the skill in,
  as for `outside_skills_found`), or a `path = "../../libs/..."` entry under
  `[tool.uv.sources]` in its `pyproject.toml`, which resolves nowhere else.
  For `app-schema`, which is how an app built here gets the app SDK, have the
  worker redo "Have the app depend on the released SDK" in its guide. For any
  other path, or when the worker reports that the app needs an SDK that is
  not released yet, stop, and tell the user the app cannot be published until
  that is changed.
- `refused`: `detail` says why (no version in the manifest, a version not
  newer than the last published, a manifest that fails its check). Have the
  worker fix it.

## When the user calls it off

Before the push, calling the publish off leaves nothing on GitHub. Destroy the
worker and its branch, without merging it, so the workspace's copy of the app
stays as it was:

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py destroy --name <slug>
git worktree prune
git branch -D "mngr/<slug>"
```

`-D` because git refuses `-d` for a branch that was never merged.

## After a push

`pushed` means the version is on GitHub. On a first publish the push created
the repository (`is_private` says whether it is private), closed its issues, wiki, projects, discussions and
outside pull requests, and gave it the topic `imbue-studio-app`. Before
step 8:

- **Check `is_tag_pushed`.** When it is false the version has no tag, and
  whoever installs or updates the app is given the highest tag, so they would
  keep getting the version before this one. Run the push again now (it sends
  only the tag), because step 8 ends the publish and nothing sends the tag
  after it.
- **If the tag still does not go**, or `problems` says the repository already
  has that tag (someone made it on GitHub, and it is never moved), stop and
  tell the user, and run step 7 and step 8 once it is sorted out.
- **Every other entry of `problems`** is a follow-up to mention (a setting
  that did not take), not a failed publish.

## When the push stops

- `name_taken`: the account already has a repository of that name. Nothing
  was published. Go back to the scope gate for the name alone: agree another
  with the user, have the worker point the README's links at it, run step 5
  again, show the README again, and push with the new `--repo-name`.
- `push_refused`: the published repository holds changes this workspace did
  not make (someone edited it on GitHub, or another workspace publishes the
  same app). Nothing was pushed. Stop and tell the user; never try to
  overwrite it.
- `not_assembled`: run step 5 first.
- `refused`: `detail` says why, with what to do when the cause is known (the
  push permission is missing: request it again as `github-access.md` says,
  then run the push again). Run it again once the cause is fixed: a
  repository that was created is not created twice, and a version that
  reached the repository is not pushed twice.

## When recording stops

The app is published whatever `publish record` answers, so never push again.

- The step was interrupted, or `problems` say the record was not saved: run
  `uv run --no-sync app-install publish record <name>` again.
- `not_assembled` after a run that answered `recorded`: there is nothing left
  to record (`app-install list` shows the app's `published_version`).

## Making a published app public

A published app's repository is private unless the user chose public when it
was first published. Making it public later needs no new version: everything
in the repository went through the secret scan before it was pushed.

```bash
uv run --no-sync app-install publish visibility <name>
```

says who can see it now (`is_private`). To make it public:

1. Tell the user, in one message, that anyone will be able to see and install
   the app from then on, that this cannot be taken back by you (they can make
   it private again themselves on GitHub, but copies people took stay out
   there), and that it is shared under the MIT license. End your turn and
   wait for an explicit yes to THAT message.
2. Run `uv run --no-sync app-install publish visibility <name> --make-public`.
   `is_changed` is true when it was private and is public now. On `refused`,
   `detail` says why: an app that was never published has no repository to
   make public, and a missing GitHub permission is requested as "Asking for
   the access" in `github-access.md` says.
3. Give the user the address again.
