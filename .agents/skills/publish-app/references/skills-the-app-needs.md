# Skills the app needs

A published app has to work in a workspace that has nothing of this one. A
skill built for the app (the one that fetches and processes its data, the one
that drives it) lives under `.agents/skills/` until now, where whoever
installs the app would not have it. Publishing is when the app takes it in:
the skill moves into the app's own directory and is published with it. This
file is for you, the lead; the worker's part is in `scan-worker.md`. Step
numbers are SKILL.md's.

## Contents

- Which skills
- A skill something else uses too
- At the scope gate
- In the worker's task
- After the version is published
- When nothing was published

## Which skills

Four places name them. A skill named in any of them is a candidate:

- `outside_skill_uses` in `compare`'s result: each line of the app that names
  a skill by its path (`.agents/skills/<skill>/...`, `data/.skills/<skill>/...`).
  A publish is refused while one remains.
- A skill's package the app imports or lists as a dependency:

  ```bash
  git grep -n -E '_skill\b|-skill\b' -- system/apps/<package>
  ```

- A skill one of the app's routines runs. A routine names its skill by name,
  never by path, so nothing above finds it:

  ```bash
  git grep -n -e '^skill = ' -- system/apps/<package>/routines
  ```

- A skill the app's record lists under `[[references]]`
  (`system/config/apps/<name>.toml`): one that was built for the app or calls
  it.

Then decide, for each:

- **The app cannot do its job without it** (the first three places): the app
  takes it in. There is no publishing around it.
- **It only drives the app** (the last place alone) and would be useless
  without it: the app takes it in, so whoever installs the app gets it too.
- **It is useful without the app**: it stays where it is, and is not
  published. Say so at the scope gate.

A skill the template ships (`datalib`, `notify-user`) is never taken in: every
workspace has it. An app that reads what `datalib` mirrors needs the Datalib
app where it is installed, which the worker writes under `[[dependencies]]`.

## A skill something else uses too

Before the scope gate, find what else uses each skill the app would take in:

```bash
git grep -l -e '.agents/skills/<skill>' -e '<skill_with_underscores>_skill' -e 'data/.skills/<skill>' \
    -e 'skill = "<skill>"' -- . ':!.agents/skills/<skill>' ':!system/apps/<package>'
```

The last pattern is a routine that runs the skill, which names it without a
path.

- **Nothing else, or only a routine or a doc**: the app takes it in, and the
  worker repoints what names it.
- **Another app uses it**: only one app can carry a skill. The app that owns
  the data carries it, and every other app calls that app (its API, found by
  name) and lists it under `[[dependencies]]`. When it is clear which app
  that is (the skill fetches Slack messages, and one of the two apps is the
  Slack app), propose it; otherwise ask the user which app the data belongs
  to. If the answer is the other app, publish that one first: this app then
  depends on it and takes nothing in.
- **Neither app is the natural owner**: stop here, before the scope gate, and
  offer to make the data an app of its own (the `build-app` skill: an app
  with no window that serves the data over its API), which both apps then
  call. Publish that app first.

## At the scope gate

Add to the one message, in the user's terms:

- that this part of the workspace ("the part that collects your Slack
  messages") becomes part of the app and is published with it, and each skill
  that stays behind and is not;
- that its command changes, when the user runs it by name: an app's skill is
  named after the app (`/<name>-<skill>`), so two apps from different people
  never clash where both are installed;
- for a skill another app uses: which app will own it, and that the other
  will ask this one for the data from now on.

Nothing changes in the workspace unless the version is published.

## In the worker's task

The task file's `skills to take in` line names each skill, and after it
everything else that uses it (the paths the `git grep` above printed), so the
worker repoints them in the same change. `none` when there is none.

## After the version is published

Step 8 merges the worker's branch, and with it the skill's move. Then, before
you tell the user it is published:

1. **The workspace's own packages.** When the merge changed the root
   `uv.lock` (a skill with a Python project of its own left the workspace's
   lock), run `uv sync --all-packages --frozen`.
2. **The data the skill kept.** The worker's report names each directory the
   skill's data moves between (`data/.skills/<skill>/` to a directory under
   `data/.apps/<name>/`). Data is not in git, so the merge did not move it.
   Stop the app's programs (`supervisorctl stop <name>`, and each
   `<name>-<role>`), move the directory (`mv`, never a copy: it can be
   large), and start the app again with
   `uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart`.
   An app's data is what an update copies aside and an uninstall keeps, and
   only what is under `data/.apps/<name>/` counts as that.
3. **Check it on the user's data.** Open the app and confirm it still shows
   what it showed before, then run the skill once under its new name.
4. **Tell the user** the skill's new command, when they run it by name.

## When nothing was published

The move lives only on the worker's branch until step 8, so a publish that
stops or is called off leaves the skill where it was and the app as it was.
