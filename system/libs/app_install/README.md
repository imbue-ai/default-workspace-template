# app_install

The `app-install` command: what registers an app a workspace's agent builds, and what keeps the files the workspace needs for it in step with its manifest.

A user app is one self-contained directory, `system/apps/<package>/`.
Everything the app owns is inside it: its code, its skills (`skills/<name>/`), its routines (`routines/<id>.toml`), and its environment units (`env.d/<NNNN>-<name>.sh`).
The command is deterministic and asks nothing; every run ends in one JSON line on stdout.

```bash
uv run --no-sync app-install add <package> [--port <n>]
uv run --no-sync app-install install <url> [--branch <ref>]
uv run --no-sync app-install update <name> [--branch <ref>] [--continue]
uv run --no-sync app-install regenerate [<name>]
uv run --no-sync app-install uninstall <name>
uv run --no-sync app-install migrate <package> [--dry-run]
uv run --no-sync app-install link <name> [--refresh] [--create-dev-repo]
uv run --no-sync app-install reference add <name> <path> [--note <text>]
uv run --no-sync app-install reference remove <name> <path>
uv run --no-sync app-install publish compare <name> [--commit <rev>]
uv run --no-sync app-install publish set-version <name> <version>
uv run --no-sync app-install publish assemble <name> [--commit <rev>]
uv run --no-sync app-install publish push <name> [--repo-name <repository>]
uv run --no-sync app-install publish record <name>
uv run --no-sync app-install verify <name> [--template <version>]
uv run --no-sync app-install check
uv run --no-sync app-install list
```

Installing, updating, and linking an app go through `git subtree`, which some distributions package apart from git.
Each of the three checks for it first and is refused with a message naming the missing command (`refused`, with `git-subtree` in `detail`) rather than with git's usage text.

## The app record

Each user app has one tracked file, `system/config/apps/<name>.toml`: the app's name, its directory, the loopback port of its main program, whether this workspace built it (`origin = "local"`) or installed it, and the repositories it is kept in step with.
It is one file per app so that two branches that each add an app never edit the same file.
An app under `system/apps/` without a record is a built-in app.

What else in the workspace uses the app is listed in its record too, under `[[references]]`: a skill that calls the app's API, a script that reads its data.
A change to the app then reaches them, since the footprint of the app includes them.
`app-install reference add <name> <path> [--note <text>]` lists a path and `app-install reference remove <name> <path>` takes one out; each writes the record and commits that one file under the installer's own author (`reference_added`, `reference_removed`, with the record's `references` as they are now).
A reference is a literal path that exists, outside every app's directory, reached through no symlink, and not under `system/vendor/` or `data/`; one that breaks a rule, and one the record already lists, is `refused` with the reason in `detail`.

`app-install list` prints every record with two more facts about the app:

- `state`: `owned` for an app this workspace built, `installed` for one added from someone else's repository, and `modified` for an installed app whose committed directory differs from the tree it was installed from (the record's `upstream_tree`) in any path outside the regenerable set.
  The regenerable set is the files in an app's directory that are made from other files there, which today is `uv.lock`: an app whose lock was only made again here is still `installed`.
  An edit that is not committed yet changes nothing.
- `is_linked`: whether the workspace history holds a join marker for the app's directory (see "Linking an app" below).

## Derived files

The workspace needs two kinds of file outside an app's directory, and both are generated from the app's manifest and record rather than written by hand:

- `system/supervisord.conf.d/<program>.conf`: one file per program. The app's main program runs the manifest's `[run]` command (by default the console script named after the app) with the app's port in its environment, after registering the manifest and the port through `forward_port.py`. Each `[[services]]` entry is the program `<name>-<role>`. A program of a Python app first links and syncs the app's own virtual environment from its lock, and a declared secret file reaches the program through `with_secrets.py`.
- `.agents/skills/<skill>`: a symlink to each skill the app carries, which is how a harness discovers it.

They are tracked in the workspace repository and never enter an app's own repository.
Do not edit one: change the manifest and run `app-install regenerate`.
`app-install check` fails when one differs from what the generator writes, and `system/test_app_manifests.py` runs it.

## Ports and names

A port is the lowest one at or above 8080 that no supervisord program, no registry row, and no app record uses, that is not one of the two ports the Screen holds without a program line (8083 and 8091), and that nothing is bound to.
The port reaches the app through its environment, so no file in the app's directory holds it; anything that calls an app finds its URL by name, with the app SDK's `resolve_app_url`.
Two branches can each allocate the same port; `check` reports it and `regenerate` gives a new port to the app whose record was committed later.

An app is refused before anything is written when it would take a name something in the workspace already holds: the app's name, a program, a console script, a skill, a routine id, or an environment unit's file name.
An app's name may not be another app's name followed by a hyphen and more, or the start of one: an app's background programs are named `<app name>-<role>`, so one of the two would be taken for a program of the other.
The result names every collision.
`add` also refuses a manifest that carries `[[references]]` or `[wiring]`, which an app with a record does not declare: what else uses the app is listed in its record, and its background programs are `[[services]]`.
`regenerate` is refused the same way (`collision`) when a skill an app has gained would take the place of a skill the workspace or another app already holds under that name.

## Removing an app

`app-install uninstall <name>` removes the app's directory, its record, and its derived files, has supervisord drop its programs, removes its registry row, and commits the removal under the installer's own author.
What the installer and the sync service kept about the app on this machine goes with it (`removed_state_paths`): an update that was waiting for a person, with its worktree, a publish that was waiting between two of its steps, and what was last pushed to the app's dev repository.

Everything else is left, and the result names it, because a later app of the same name takes it over:

| Field | What is left |
| --- | --- |
| `kept_paths` | The app's stored data (`data/.apps/<name>/`), the newest copy of it an update made, and its secret files. |
| `kept_share_grants` | The people the app was shared with: the file, the app's table in it (`services.<name>`), and how many people, addresses and domains it names. `null` when the app was shared with nobody. |
| `kept_routine_state` | The state directory (`data/.state/routines/<id>`) of each routine the app owned: its on/off choice, the schedule the user gave it, and its runs. |
| `kept_repositories` | The app's repositories on GitHub, its dev repository and its published one. Nothing on GitHub is deleted or changed. |
| `dependents` | The apps still in the workspace whose manifests list this one under `[[dependencies]]`. Dependencies are advisory, so the uninstall goes ahead. |

What it could not do outside the tree, such as reaching a supervisord that is not running, is listed under `problems`; the app is removed from the tree all the same.

## Converting an app built before this layout

`app-install migrate <package>` turns an app that has a manifest and a hand-written program file, and keeps its skills and routines elsewhere in the workspace, into a user app.

- A skill the manifest's `[[references]]` names moves into the app's `skills/`, with a link left where it was, unless another app names it too or it has a Python project of its own; those, and every other reference, are listed in the record's `references`.
- A routine whose `app` is this app moves into the app's `routines/`.
- The manifest loses `[[references]]` and `[wiring]`, and its `[scope] exclude` globs become relative to the app's directory.
- The record keeps the port the app already runs on, and the program file is replaced by the generated one.

A conversion either completes or changes nothing.
An app whose existing program file is not what its manifest generates (it runs under a secret file the manifest does not declare, say, or with a command of its own) is left exactly as it was, and the result says where the two differ: declare how the app runs in `app.toml` (`[run]`, `[[services]]`, `[[secrets]]`) and convert it again.
`--dry-run` prints what would happen.

## Installing an app from a repository

`app-install install <url>` adds an app someone published to the workspace.
It takes the tag or branch named by `--branch`, else the repository's highest `v<major>.<minor>.<patch>` tag, else its `main` branch, and refuses a repository with no `app.toml` at its root (`not_an_app`).

The app enters the workspace repository with `git subtree add --squash`, so the publisher's history stays out, in a worktree of the installer's own; its record and derived files are written there, and the result reaches the primary checkout as one landed commit.
A refusal, such as a collision with something already in the workspace, leaves the primary checkout untouched, and so does the user's uncommitted work.
The record keeps the repository's URL, the commit that was taken, that commit's tree, and the app's version, and the ref `refs/app-upstream/<name>` keeps the commit's objects in the repository.

The repository is read anonymously first, and through the workspace's latchkey gateway when that is refused, whether or not GitHub sync is enabled; `needs_github_access` means neither reached it and the GitHub read permission has to be granted first.

After landing, the command installs the system packages and tools the app's `[environment]` declares, has supervisord start the app's programs, and waits for the app's port to answer.
Its result lists what the app still needs from the user under `needs`: the permissions and secret files it declares (and whether each file exists yet), whether it calls a model, its adaptation notes, the apps it expects that are not here, and anything of its environment that could not be installed.
A program that runs under a secret file that does not exist yet cannot start, so its port is not waited for.

## Updating an installed app

`app-install update <name>` brings a newer version of an installed app in from the repository it came from, over the workspace's own changes to the app, with `git subtree merge --squash`.
The live app is untouched until the result lands.

- The app's stored data is copied to `data/.state/app_install/snapshots/<name>/` first, out of the backup, and only the newest copy is kept. When the data does not fit on the disk beside itself, the update goes ahead only if the workspace has a recent backup (`no_snapshot` otherwise). A copy that fails, over a file the running app removed meanwhile say, also ends with `no_snapshot`, with the earlier copy left in place.
- A conflict in `uv.lock` alone is settled by taking the publisher's lock and making it again. Any other conflict ends the run with `conflict`, the files, and the worktree the merge waits in, `data/.state/app_install/worktrees/<name>`: resolve it there, commit, and run the update again with `--continue`.
- The merged app is registered and regenerated on the port it already has, and its checks are run (its environment syncs from its lock and its tests pass). `tests_failed` leaves the worktree for a fix, and `--continue` picks it up.
- Then the result lands as one commit, the record takes the new commit, tree, and version, anything new in `[environment]` is installed, and the app's programs are restarted. A result that does not merge into the primary branch, which changed the same lines while the update waited, is refused with the primary checkout left as it was: merge the primary branch into the waiting worktree, commit, and run the update again with `--continue`.

To give a waiting update up, remove its worktree (`git worktree remove --force data/.state/app_install/worktrees/<name>`): the live app was never touched, and the next `update` starts afresh.
That is also the way out when the merge was taken back in the worktree (`git merge --abort`): `--continue` then answers `conflict` and records nothing, since the new version is not there to land.

`up_to_date` means the app is already at the version asked for.
An app this workspace built has nothing to update from.

## Recording that an app's checks pass

`app-install verify <name>` runs the app's own checks (its environment syncs from its lock and its tests pass) and, when they pass, writes the template version they passed on to the record's `verified_template` and commits that one file (`verified`).
The version is the nearest `minds-v` release tag in the checkout's history, or the one `--template` gives: the update pass names the version it is bringing in, which its worktree's history holds no tag for yet.
Checks that fail write nothing (`tests_failed`).
An adopter's agent compares an app's `verified_template` with its own workspace's version when the app misbehaves there.

## Linking an app

An app can have a private repository of its own on GitHub, `<name>-dev`: the app's directory at its root, with the app's own commit history.
The `github-sync` service fills it and keeps it current with `git subtree split`, which turns the workspace commits that touched the app into commits of the app's own history (`system/libs/github_sync/README.md`, "App dev repositories").
`app-install link <name>` does the two things the service never does: it commits to the workspace repository, and it creates a repository.
Run it from the workspace root on the primary checkout; in a linked worktree it is refused, since it moves the primary branch.

**The join marker.**
Without one, a split walks the whole workspace history, which takes minutes.
For an app this workspace built, `link` makes the app's first split with `git subtree split --rejoin`, which leaves a marker: a merge commit whose message carries `git-subtree-dir: system/apps/<package>`, whose first parent is the commit it was made on and whose second is the split.
A later split starts from the newest marker.

- The marker is made in a clone under `/var/tmp`, never in the checkout, with hooks off, and the primary branch is then moved to it.
  Its tree is its first parent's tree, so no file changes and uncommitted work is left exactly as it is.
  The branch is moved only if it is still where the marker was made; when something else committed meanwhile, the marker is made again.
- The clone is shallow, cut one second before the oldest commit that touches the app (`git clone --shallow-since`), so the first split walks the app's lifetime rather than the workspace's.
  That is used only when the app's history reads the same in the clone as in the workspace: the same commits touching the app's directory, each following the same ones (`git rev-list --parents <commit> -- system/apps/<package>`).
  It does not when a commit is dated earlier than one of its ancestors, and when a merge made after the app's first commit brings in a commit dated before it: git cuts every parent of such a merge, so the merge reads as the commit that added the app.
  The marker is then made in a clone of the whole history, which is correct and slow, and the result says so (`is_full_clone_used`).
- **An app is linked** when the history of the checked-out commit holds such a marker: `git rev-list --max-count=1 --grep='^git-subtree-dir: system/apps/<package>/*$' HEAD` names a commit.
  This is the search `git subtree` itself makes, and `link`, `list`, and the sync service all ask it through one function, `github_sync.app_history.is_app_linked`.
  Linking an app that is linked makes nothing.
- `--refresh` makes a new marker at the current commit when a commit has touched the app since its newest marker, which keeps later splits fast as workspace commits accumulate.
  When the app has not changed since, there is no newer split to join and none is made: `git subtree` cannot read a history that holds two markers for one split.
- An installed app needs no marker.
  The commits `install` and `update` leave are join markers already, so `link` only creates its dev repository, and only once the app is `modified`; an unchanged installed app is refused.
- An app whose directory was removed and made again (an app uninstalled and built or installed again under the same name) is refused.
  `git subtree split` keeps the commit that removed the directory as it is, and everything the workspace held before it comes along, so the split would carry the workspace's own history, with every file of it, into the app's.
  `link` and the sync service both check that a split's history does not hold the workspace's first commit (`github_sync.app_history.require_split_holds_only_the_app`), and neither joins nor pushes one that does.

**The dev repository.**
When GitHub sync is enabled for the workspace (`data/system/github_sync.toml` exists) and the app's record has no `dev_repo_url`, `link` creates the private repository `<name>-dev` under the workspace's GitHub account through `latchkey curl`, closes it to outside contributions as a published template's repository is closed, writes its URL to the record, and commits that one file under the installer's own author.
`--create-dev-repo` does the same in a workspace where sync is not enabled, for one app on request; `uv run --no-sync github-sync sync-app <name>` then pushes the app once, and again whenever it is run.
A repository that cannot be created (the name is taken on the account, or the GitHub permission is missing) does not undo the marker: it is listed under `problems`, and the next `link` tries again.

The result is `linked` (the first marker was made), `refreshed` (a new one was made), or `already_linked` (none was made), with:

| Field | Meaning |
| --- | --- |
| `marker_commit` | The marker that was made, else empty. |
| `split_commit` | The commit of the app's own history the marker joins, else empty. |
| `is_full_clone_used` | Whether the marker was made in a clone of the whole workspace history. |
| `dev_repo_url` | The record's dev repository, empty when the app has none. |
| `is_dev_repository_created` | Whether this run created it. |
| `remaining` | What is left before the app is synced, in sentences: for example "GitHub sync is not enabled, so no dev repository was created; pass --create-dev-repo to make one for this app alone". |
| `problems` | What this run tried and could not do. |

## Publishing an app

An app's published repository is public, is named after the app, and holds one commit per published version: the app's directory at one workspace commit, less the paths the manifest's `[publish] exclude` names.
It is what another workspace installs from.
A publish is driven by the `publish-app` skill, which asks the user twice before anything becomes public; the steps that need no judgement are five subcommands, run in this order.

| Step | What it does | Result |
| --- | --- | --- |
| `publish compare <name> [--commit <rev>]` | Says what a publish of that commit would hold (`paths`, `excluded_paths`) and which files differ from the version last published (`changed_paths`, each `added`, `modified` or `deleted`), which the next version's number is chosen from. Writes nothing. | `compared` |
| `publish set-version <name> <version>` | Writes the version into `app.toml`, with `verified_template` set to the template release the workspace is on, and for a Python app into `pyproject.toml`, whose lock is made again. Commits nothing. | `versioned` |
| `publish assemble <name> [--commit <rev>]` | Builds the tree in a directory under `/var/tmp`, outside the workspace, and finds it fit to publish. Pushes nothing. | `assembled` |
| `publish push <name> [--repo-name <repository>]` | Creates the repository on the first publish and writes its URL to the record, then pushes the assembled tree as one commit and tags it. | `pushed` |
| `publish record <name>` | Writes `published_repo_url`, `published_commit`, `published_version` and `verified_template` to the app's record and commits that one file. | `recorded` |

**The version.**
A version is three numbers, with an optional pre-release after a hyphen, and each published version is newer than the last.
The template release the workspace is on is the nearest `minds-v` tag in its history (`git describe --tags --match 'minds-v*'`), which is how the update pass names a workspace's version too; when the history holds none, `verified_template` is left as it was and `problems` says so.

**The assembled tree.**
`assemble` reads the app's directory at the commit it is given (`HEAD`, or the commit of the branch a publish's worker worked on), in an index of its own, so the checkout and whatever is uncommitted in it are not touched.
`[publish] exclude` is gitignore syntax, relative to the app's directory.
The tree is refused, with nothing left waiting, when:

- its manifest fails the authoring check, names no version, or names one that is not newer than the last published (`refused`);
- the secret scan finds anything in it (`secrets_found`, with each finding under `findings`: the scanner, the rule, and the file, never the value). The scan is the one a template publish runs, `.agents/skills/publish-template/scripts/scan_secrets.sh`, and a scan that could not run to the end stops the publish too;
- it holds a `pyproject.toml` whose lock is not what the project calls for (`lock_check_failed`). `uv lock --check` runs in the assembled directory, so an app that resolves only inside a workspace does not pass;
- the app was installed and nobody has changed it, so it is its publisher's to publish (`refused`).

The result names what an adopter will be asked for under `needs` (`permissions`, `secrets`, `llm`, `environment`, `dependencies`, `adaptation`).

**The push.**
On the first publish the public repository is created under the workspace's GitHub account through `latchkey curl`, closed to outside contributions as a published template's repository is, and given the topic `imbue-studio-app`.
Its URL is written to the app's record and committed at once, so a push that fails afterwards never creates it a second time.
A name the account already uses stops the publish with `name_taken`: the app's `README.md` names the repository, so the name is settled with the user again before anything else.
The commit is made with `git commit-tree`: the assembled tree, the commit last published as its parent (none on the first publish), and the message `<name> v<version>`.
The first publish of an app that was installed and then changed here adds the trailer `Derived-From: <upstream_url>@<upstream_commit>`.
It is pushed to `main` as a fast-forward and the tag `v<version>` after it, both through the latchkey gateway and neither forced: a repository that holds commits the publish does not continue ends the run with `push_refused`.
A tag that does not reach the repository is listed under `problems`; the version is published all the same.
The ref `refs/app-published/<name>` keeps the published commit in the workspace repository for the next publish to follow; a workspace that lost it fetches it from the published repository.

**Between the steps.**
What `assemble` built waits in `data/.state/app_install/publish/<name>.json`, and `push` adds the commit it made and whether that commit and its tag have arrived.
So a step that was interrupted is finished by running it again: `push` makes the commit once and pushes the same one again, which changes nothing in a repository that already has it, and `record`, run alone, finishes a publish whose record was not written.
`push` and `record` without a waiting tree are `not_assembled`, and `record` before a push is `not_pushed`.
`assemble` is `refused` while a version that was pushed still waits to be recorded, since assembling the next one would forget which commit that was.
`record` lists under `problems` a published version made from a commit that is not in the checkout's history yet, which means the branch that holds it still has to be merged.
