# Worker: impact analysis (the worker guide's Step 4a)

Read this when `classify-merge` reports `has_local_footprint: true`, or when
a routine or cron entry named something the update retires. It finds every
*user-created* consumer of what the update changed, in this order.

## Contents

- 1. Scope files: the user's creations by footprint
- 2. Retired names
- 3. Dependents of each changed file
- 4. Provisioning files and global dependencies
- What to record

## 1. Scope files: the user's creations by footprint

Every app with an `app.toml` has a footprint the manifest library computes:
its directory, the supervisord drop-ins that run it, and the paths its
manifest claims in `[[references]]`. Reading it over two ranges answers by
rule what 4a and 4b otherwise judge by hand: whether the creation carries the
workspace's own content, and whether the update reached it.

```bash
eval "$(uv run --no-sync .agents/shared/scripts/agents_shared/parse_task_frontmatter.py 'data/.tasks/update-self/task.md')"
SCOPES=data/.tasks/update-self/scopes
RANGES=data/.tasks/update-self/footprint-ranges.json
rm -rf "$SCOPES" && mkdir -p "$SCOPES"
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    footprint-ranges --target "$TARGET_REF" > "$RANGES" || exit 1
for manifest in system/apps/*/app.toml; do
    package=$(basename "$(dirname "$manifest")")
    uv run --frozen --package app-manifest app-manifest footprint "$manifest" \
        --diff-base "$(jq -r .local_base "$RANGES")" \
        --diff-ref "$(jq -r .local_ref "$RANGES")" \
        --out "$SCOPES/$package.local.json" || exit 1
    uv run --frozen --package app-manifest app-manifest footprint "$manifest" \
        --diff-base "$(jq -r .update_base "$RANGES")" \
        --diff-ref "$(jq -r .update_ref "$RANGES")" \
        --out "$SCOPES/$package.update.json" || exit 1
done
```

`footprint-ranges` names the two ranges: the local range is what the
workspace itself changed since it forked from the target's line, and the
update range is what the update changes in the tree the live workspace runs.
Both are anchored on this pass's merge commit rather than on `HEAD`, so a fix
you commit on top of it, and any rerun, reads the same two sides. The command
shifts them for a pass after rolled-back updates, and refuses when the latest
merge commit it finds does not merge `$TARGET_REF` (an earlier update's merge
carries the same subject). `--package app-manifest` installs the library from
the merged tree, and `--frozen` keeps the command from re-locking the merged
tree before 4b's environment gate has checked it.

How to read the two files of a creation:

- `<package>.local.json`'s `diff.inside_footprint` is the creation's own
  content: every file of its footprint in which the workspace differs from
  the base (all of them for an app built here, the modified ones for a
  built-in app the user changed). Empty means the creation is upstream's
  exactly as shipped, so the footprint evidence names no customization of it
  to carry. It does not exempt the app from any gate the rules apply to the
  merged set.
- `<package>.update.json`'s `diff.inside_footprint` is what the update
  changed inside that same footprint.
- A creation with both non-empty is one **the update touches**: name it and
  its `references` as consumers, and 4b's customization survival covers it
  over exactly the files the `.update.json` lists.
- A creation with local content that the update did not reach by footprint is
  still a consumer to find by search and interface coupling (section 3): the
  footprint is what the app owns, not what it depends on.

Two limits of this evidence:

- The footprint is read from the merged tree, so the local side is measured
  against the merged manifest. A local edit under a path the update removed
  from the manifest (a dropped `[[references]]` entry, a program moved out of
  the app) falls outside it, and the manifest's own diff in `.update.json` is
  the sign to look.
- The update range is what the merge changed against the local side, so a
  conflict you resolved wholly to the local side shows no update change
  there. The discarded-side accounting of your conflict triage carries that
  case.

A `footprint` command that fails names a manifest the merged tree can no
longer satisfy, most often a `[[references]]` path the update deleted or
moved. That is merge work: fix the reference in your branch (the root suite's
`system/test_app_manifests.py` holds every manifest to it) and rerun the
block. Never drop the creation from the loop.

A workspace-added skill has no footprint the update can reach at all, and
stays a consumer for sections 2 and 3 to find. An app directory with no
`app.toml` is read by hand (`older-workspaces.md`, "Apps from before the app
model").

## 2. Retired names

A creation calls the template by name: it runs a command, passes a flag,
reads an environment variable, connects to a port, requests a route, or runs
a skill. When a release removes or renames that name, nothing imports it and
no test of the template fails; the creation breaks the next time it runs. So
enumerate every name the update removes or renames, and search the user's
creations for each.

**Enumerate** from four sources, and write one name per line to
`data/.tasks/update-self/retired-names.txt` (no blank lines: an empty pattern
matches every line of the search below):

```bash
eval "$(uv run --no-sync .agents/shared/scripts/agents_shared/parse_task_frontmatter.py 'data/.tasks/update-self/task.md')"
BASE=$(cat data/.tasks/update-self/base.txt)
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    retired-names --base "$BASE" --target "$TARGET_REF"
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    changelog-entries --base "$BASE" --target "$TARGET_REF"
git diff --name-status --diff-filter=DR "$BASE" "$TARGET_REF"
```

- **`retired-names`** lists what the tree itself declares. Each of its
  `removed_entry_points` is a command a `pyproject.toml` installed
  (`[project.scripts]`) that no project installs at the target; its
  `same_target` names any command at the target that runs the same code,
  which is what a rename looks like. `removed_skills` and `renamed_skills`
  are the directories under `.agents/skills/` that are gone, the renamed ones
  with their new names. Read by hand any manifest it lists under
  `unreadable_pyprojects`.
- **The template's own changelog entries** between the two releases, which
  `changelog-entries` lists. Read each one for what it says was removed,
  renamed or replaced: a command, a flag, an environment variable, a port, a
  route, a skill, a file. Note the replacement an entry states.
- **mngr's changelog entries**, when the update moves the mngr pin
  (`pyproject.toml`, `[tool.uv.sources]`). That pin is the largest surface an
  update moves, and mngr's entries between the two commits announce removed
  behavior in prose that no test failure surfaces. Read them in the public
  repo at https://github.com/imbue-ai/mngr, the same way.
- **Deleted and renamed paths**, which the `git diff` line lists.

**Search** the workspace's own content for each name. The files that are the
workspace's own are the ones the merged tree holds differently from the
target release, added or modified:

```bash
eval "$(uv run --no-sync .agents/shared/scripts/agents_shared/parse_task_frontmatter.py 'data/.tasks/update-self/task.md')"
git diff -z --name-only --diff-filter=AM "$TARGET_REF" HEAD \
    | xargs -0 -r grep -n -H -F -f data/.tasks/update-self/retired-names.txt --
```

Search the routines as the worker guide's Step 4a does, and a cron entry
where one may remain. A name that is a common word matches lines that do not
use it: read each hit before you count it.

**Dispose** of each hit:

- **The release states a replacement**: a command `retired-names` pairs with
  a `same_target`, a skill in `renamed_skills`, or a replacement a changelog
  entry names. Fix it in your branch: rewrite the use to the new name and
  commit. That commit is an edit of your own, so it selects the creation's
  suite in 4b and is merge work for 4c.
- **Any other hit**: a name removed with no replacement, or a replacement
  that changes what the creation would do. Do not guess a rewrite. Report it
  under Impact analysis (the creation, the name, what the release says about
  it), and list the creation in `impacted-paths.txt` (section 3) so its
  tests run.

## 3. Dependents of each changed file

Exploration work, for every changed `system/scripts/**`, `system/libs/**`,
`system/services/**`, `system/apps/**`, `system/vendor/**`, and `.agents/**`
path.

1. **Enumerate the consumer universe** up front: every
   `system/supervisord.conf.d/` program (and what its `command` invokes),
   every app or service under `system/services/` and `system/apps/`, every
   workspace-added skill under `.agents/skills/`, and every routine. Start
   from the scope files: they already name every manifest app with local
   content and, through `references`, the skills, scripts and docs each
   claims, which is the part of this list that is declared rather than
   discovered.
2. **Search for dependents of each changed file**: its path, basename, and
   importable module name. Follow each service's code into the shared scripts
   and libs it calls. Check skills' `SKILL.md` and scripts, and the paths an
   app's `app.toml` claims in `[[references]]`. For a manifest app with a
   scope file that last search is already done: its `.update.json` lists the
   update's changes inside its footprint, references included, and a hit
   there is an impacted consumer. If the update adds reference- or
   dependency-declaration machinery, write the declarations it expects.
3. **Reason about interface-level coupling no grep finds**: an API surface
   (the shell's HTTP API, a shared data file's format, a script's CLI flags)
   has callers that reference no file of it.
4. **Bias toward "impacted" when uncertain**, and record what you checked and
   how, so the lead sees the coverage. List each impacted creation in
   `data/.tasks/update-self/impacted-paths.txt`, one line per creation,
   naming a file that stands for its whole suite (an app's `app.toml`, a
   skill's `SKILL.md`, a package's `pyproject.toml`, or a flat script
   itself), so its tests run in 4b's gate.
5. **Verify "workspace-added" by provenance, not directory**: a path is
   built-in if it exists at the target ref (`git ls-tree -r --name-only
   "$TARGET_REF" -- <dir>`; empty output = genuinely workspace-added).
   Mislabeling built-in code as the user's misattributes pre-existing issues.

The apply restarts every supervisord program on every apply, so built-in
services need no naming for a restart. What only your analysis can name is a
*user-created* consumer: an app, widget or skill of the user's that reads
something the update changed. An impacted service still gets validated in 4b;
an impacted skill gets validated per its own contract.

## 4. Provisioning files and global dependencies

**Provisioning files** (`system/scripts/setup_system.sh`, the installers it
chains, `.mngr/**`) and **global-dependency bumps** always count as impacted
even though nothing imports them. Work them per
`worker-provisioning-changes.md` and report each there.

## What to record

Under your report's Impact analysis: the scope-file evidence (which creations
carried local content, which the update reached), the retired names you
enumerated and what each search found, each fix you committed for a stated
replacement, each hit you report instead, and what else you checked and how.
