---
name: publish-template
description: Publish a clean, bootable snapshot of what this agent built -- an app or feature, a skill, a chat behavior, a workflow, a service, config or seed data -- to a new GitHub repo on the user's account, as a template another agent can be created from. Use when the user asks to publish, share, or export something they built as a reusable template. For a new version of a template already published, use update-published-template.
metadata:
  author: imbue
---

# Publish a template

A template is a clean, **bootable** snapshot of what this agent built, published
to a GitHub repo so another agent can be created FROM it, not merely read it.
Each repo holds exactly one template: `template.md` (prose), `template.toml`
(machine-readable: recipe, requirements, environment, lineage) and
`template.svg` (thumbnail), at the root of the full workspace tree. This is flow
version v2; adopters still read v1 repos (a slug-named `inspiration-<slug>.md`
and no TOML), but nothing writes them.

A background worker assembles the snapshot in its own worktree; you run the
conversation, the GitHub side and the push.

## Three rules the whole flow rests on

1. **After assembly, nothing touches the live workspace.** From the worker's
   `done` report on, every command runs with cwd = `$WT` (the worker's
   worktree), never `/home/user/workspace`, and `$WT`'s tree is what gets
   published. Never merge the worker's branch -- ignore `lead-proxy.md`'s default
   `done` handling here -- and never check it out, reset to it, or run a tree
   operation from `/home/user/workspace`: the assembled tree looks nothing like
   the workspace's HEAD, and a merge of it once deleted 1400+ files from a live
   agent. Reading the skill's scripts from the live workspace (`$PT` below) is
   fine. The one write is step 9's ledger entry, a single-file commit made by a
   script, and only after a successful push.
2. **Bootable or nothing.** What ships is always the full tree the assembly builds:
   the clean template base plus the selected paths. If any step fails, fix the
   real cause and retry it, or stop and tell the user nothing was published --
   `references/failures.md` covers each failure. Never improvise a smaller publish
   (an app directory alone, files uploaded through the API, an orphan commit).
3. **Two gates only the user can pass.** The scope gate (step 1) comes before any
   assembly, and the publish gate (step 5) before the repo exists. Each needs an
   explicit reply to THAT message: the original "publish this", an earlier gate,
   and approving a GitHub permission do not count, and your own checks are
   verification, not confirmation. End your turn after sending each one.

## Values carried through the flow

- `slug`: the title lowercased, each run of characters outside `[A-Za-z0-9._-]`
  collapsed to `-`, leading and trailing `-` stripped. `repo_name` defaults to it;
  any name the user picks must match `^[A-Za-z0-9._-]+$` and not start with `-`.
- `BASE_REF` and `SOURCE_SHA`: step 2.
- `$WT`: the worker's worktree, step 4.
- `PT=/home/user/workspace/.agents/skills/publish-template/scripts`: this skill's
  tools. Always run them from here; the copies inside `$WT` are the base's.

Note each value when you resolve it. Every command runs in a fresh shell, so a
variable set in one is empty in the next: set the ones a command uses at its
start (`PT=...; WT=...; python3 "$PT/..." ...`).

## Steps

```
- [ ] 0. Somewhere to publish to (and start the GitHub approval)
- [ ] 1. Scope Q&A, then the scope gate          -- end turn, wait
- [ ] 2. Resolve BASE_REF and SOURCE_SHA
- [ ] 3. Dispatch the assembly worker
- [ ] 4. Check the worker's result
- [ ] 5. The publish gate                         -- end turn, wait
- [ ] 6. GitHub access
- [ ] 7. Create the repo
- [ ] 8. Push and tag
- [ ] 9. Record it, and check the published page
- [ ] 10. Close out
```

In the user's progress view these are a handful of plain-language steps
("Decide what to share", "Build the shareable copy (in the background)",
"Publish it to GitHub"), not this list.

## 0. Somewhere to publish to

Ask this before anything else -- an interview and a full assembly are wasted if
there is nowhere to put the result:

> To publish, we'll post the code for your template on your own account on a
> code-hosting platform such as GitHub. Do you have a GitHub account?

- **Yes:** file the two GitHub permission requests now (step 6 has them), so the
  approval waits in Imbue Studio while assembly runs. Step 6 probes first, so this
  never duplicates a grant.
- **No:** point them at <https://github.com/signup> (a free account is enough) and
  wait until they have one.
- **Another platform** (GitLab, Codeberg, self-hosted): a fair answer, but say you
  must first work out how to authenticate it, and research that before promising
  anything. Three parts are GitHub-specific: the latchkey scopes and gateway
  (step 6), repo creation and settings (`github_repo.py`), and the README's
  `https://github.com/` links. If you cannot authenticate their platform, say so
  and offer GitHub.

## 1. Scope Q&A, then the scope gate

Ask in plain language, never listing files at the user:

- **What to include** -- an app or feature, a skill, a chat behavior, a workflow,
  a service, config, seed data: anything committed in the repo tree. You turn it
  into include paths. For an app, `uv run app-manifest footprint
  system/apps/<package>/app.toml` prints its directory, its supervisord programs
  and the skills, scripts and docs it claims; propose those plus anything else the
  user named. Include a program's own `system/supervisord.conf.d/<name>.conf`,
  never `system/supervisord.conf`.
- **What data** -- personal data (anything about the user or real people: names,
  emails, accounts, messages, contacts, private notes) is left out by default.
  Non-personal data (seed, sample or reference data, fixtures, defaults, public or
  synthetic datasets) is included by default, since it is what makes the template
  useful. Anything near the line, or that you are unsure of: ask, never guess.
- **What to change in the published copy only** -- hardcoded preferences, account
  or channel names, anything they would rather not ship. Their live files stay
  as they are.
- **A name** -- propose one; it is cheap to change later.

**Something not committed** (a chat behavior that lives only in memory, this
conversation, runtime-only state) cannot go in as-is: the template is rebuilt
from the committed tree, so it would boot without the thing that made it worth
sharing. Suggest committing it first -- usually by crystallizing it into a skill
with `crystallize-creation` -- then include it like anything else.

**The scope gate.** Send ONE message saying, in plain language:

- what WILL be included (features and the non-personal data that ships with
  them, not file lists);
- what will NOT be that they might expect (their personal data, the agent's other
  apps, secrets);
- anything near the personal/non-personal line, for them to settle;
- the published-copy changes you will make, or "none";
- the proposed title and repo name, marked as adjustable;
- that the repo will be private by default.

Then end your turn and wait for their reply.

**A rename after this point never means relaunching the worker.** The worker's
name and branch never reach the published repo (step 8 publishes a fresh commit
of the final tree). Before assembly, pass the new values to the script. After
it, edit `title:` in `template.md`'s front matter AND `[template].title` (and
`slug`, for a new slug) in `template.toml` -- the validator compares them -- and
commit in `$WT`. The manifest files carry no slug, so nothing is renamed.

## 2. Resolve `BASE_REF` and `SOURCE_SHA`

In `/home/user/workspace`, with no network (never fetch or pull upstream for
this):

```bash
BASE_REF=$(uv run .agents/shared/scripts/resolve_template_base.py)
SOURCE_SHA=$(git rev-parse HEAD)
```

`BASE_REF` is the template state this workspace is on: the second parent of the
newest `update-self:` merge, or else the `Initial workspace commit` -- never the
merge itself, which carries everything the agent had built. Do not go looking
for an older "cleaner" commit. Only if the script exits 1 (no marker at all) use
the first-parent root, `git rev-list --first-parent HEAD | tail -1` -- never a
bare `--max-parents=0` lookup, which can land on a subtree's root.

Check the base can boot before spending a worker on it:

```bash
for required in pyproject.toml system/supervisord.conf system/supervisord.conf.d; do
    git ls-tree --name-only "$BASE_REF^{tree}" -- "$required" | grep -q . \
      || echo "MISSING: $required"
done
```

Anything missing: reconsider the base (walk forward along the first-parent chain
to the first commit that passes, or ask) rather than launching.

`SOURCE_SHA` is what step 9 records as the commit this version was cut from;
capture it now, before the workspace moves on.

## 3. Dispatch the assembly worker

Only after the user replied to the scope gate. Per `launch-task`, the delegation
is one step in your timeline ("Build the shareable copy (in the background)").
If an earlier attempt left a worker or branch named `<slug>`, remove it first
(`create_worker.py destroy --name <slug>`, then `git branch -D mngr/<slug>`).

Write the task file -- parameters only; the instructions are in
`references/assembly-worker.md`:

````bash
mkdir -p data/.tasks/launch-task/<slug>
cat > data/.tasks/launch-task/<slug>/task.md << 'TASK_EOF'
---
finish_report_path: data/.tasks/launch-task/<slug>/reports/report.md
---

# Task: Assemble the "<title>" template

Before anything else, read `.agents/skills/publish-template/references/assembly-worker.md`
in your checkout and follow it. Its first step replaces your checkout with the
template base, so read it now.

- slug: <slug>
- title: <title>
- description: <description>
- BASE_REF: <BASE_REF>
- include paths, and what each is:
  - <path> -- <what it is and its role>
- data paths (opted in): <path> ..., or none
- published-version modifications: <one line each: file and the change>, or "None requested."
- context: <what it does for its user, known gaps, the accounts and services it
  depends on -- what the worker needs to write a good manifest and thumbnail>
TASK_EOF
````

Launch it, then await its report in the background:

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name <slug> --template worker \
    --runtime-dir data/.tasks/launch-task/<slug>/ \
    --task-file data/.tasks/launch-task/<slug>/task.md
```

```bash
python3 system/scripts/run_in_background.py --description "Wait for the background agent" -- \
    uv run .agents/skills/launch-task/scripts/create_worker.py await \
    --name <slug> --task-file data/.tasks/launch-task/<slug>/task.md
```

Handle `question` gates and timeouts per `.agents/shared/references/lead-proxy.md`.
On `stuck`, the report quotes the script's stderr: look it up in
`references/failures.md`, tell the user plainly, and fix the input and relaunch
or stop.

## 4. Check the worker's result

On `done`, do NOT merge. Find `$WT`, and check it against the path in the report:

```bash
WT="$(git worktree list --porcelain | awk -v b='refs/heads/mngr/<slug>' '$1 == "worktree" { wt = $2 } $1 == "branch" && $2 == b { print wt }')"
```

Then check what the worker produced:

```bash
uv run --no-project --with 'pydantic>=2' python "$PT/validate_template.py" "$WT"
git -C "$WT" status --porcelain
git -C "$WT" diff --name-only "$BASE_REF" HEAD | cut -d/ -f1-3 | sort -u
```

The validator must exit 0 and the status must be empty; if not, message the
worker to finish (per `lead-proxy.md`) rather than finishing it yourself. Every
path in the diff must fall within an include or data path the user confirmed,
or be one the assembly generates (`template.md`, `template.toml`,
`template.svg`, `README.md`, the removed `docs/VERSION_HISTORY.md`). Anything
else -- another app, skill, service or data -- means the base or the include set
is wrong: re-resolve, fix and reassemble before showing the user anything.

Leave the worker running until step 10: destroying it removes `$WT`.

## 5. The publish gate

Present the publish ONCE, in plain language, with cwd = `$WT`:

- the **title** and **description**;
- the **repo name**;
- the **visibility**: private by default. If they choose public, say in the same
  breath that it ships under the MIT license;
- **what it contains**, from step 4's diff: the apps and features, in plain terms,
  so they can confirm nothing else is in it;
- **what adopting it installs**, from `template.toml`'s `[environment]` ("it also
  installs poppler-utils"), or that it needs nothing extra;
- the **published-copy changes** that were made, so they can see their removals
  happened;
- the **permissions and secrets an adopter must grant**, from the manifest's
  Requirements, in plain words: "whoever adopts it will need to connect <X> and
  <Y> -- does that look right and complete?" A gap here silently breaks adoption;
- the **thumbnail**, embedded so they see it: `![<title> thumbnail](<WT>/template.svg)`
  with the real absolute path;
- the **README**, shown as a page per `references/readme-recipe.md`, asking
  whether it reads like a good description of what they built.

Then end your turn and wait for their explicit go-ahead to THIS message.

Apply any edits they ask for in `$WT` and commit there before going on:
requirements in both `template.md` and `template.toml`; front matter quoted as
`references/assembly-worker.md` says; README rewrites shown again until they are
happy (keeping its structure and the Open in Imbue Studio button); thumbnail
changes under the same inert-SVG rules. SVG pasted by the user is never written
verbatim: strip what breaks the rules, and say what you stripped. If they abort,
stop here and leave everything in place.

## 6. GitHub access

Two latchkey scopes, never the `gh` CLI and never a token anywhere on disk:
`github-rest-api` for creating and configuring the repo, and `github-git` for the
push, which goes through the gateway's git proxy.

```bash
python3 "$PT/github_access.py" check
```

A kind reported UNKNOWN (exit 2) means the gateway could not answer. File
nothing yet. Usually it cannot reach the user's Imbue Studio app, where they
would approve a request: ask them to open the app, and check again. For any
other reason in the parentheses, `references/failures.md` says what to do. Once
it answers, file the request for each kind it reports NOT permitted yourself. Each
request must be the ONLY command in its tool call -- the chat builds the user's
approval card from it -- so two missing means two calls, back to back:

```bash
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
    -H 'Content-Type: application/json' \
    -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "predefined", "payload": {"scope": "github-rest-api", "permissions": ["github-read-user", "github-write-all"]}, "rationale": "Publish this template as a new GitHub repo on your account."}'
```

```bash
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
    -H 'Content-Type: application/json' \
    -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "predefined", "payload": {"scope": "github-git", "permissions": ["github-git-write"]}, "rationale": "Push the published template'"'"'s git history to the new repo."}'
```

Use exactly these permission names: repo creation is `POST /user/repos`, which
the narrower `github-write-repos` does not cover. Tell the user once that an
approval is waiting in Imbue Studio (two, if you filed both), then wait in the
background and end your turn; the result starts your next one:

```bash
python3 system/scripts/run_in_background.py --description "Wait for the GitHub approval" -- \
    python3 "$PT/github_access.py" wait
```

If it times out, say so and stop, leaving everything in place.

## 7. Create the repo

```bash
python3 "$PT/github_repo.py" create --name <repo_name> --description "<description>" \
    --visibility private --worktree "$WT"
```

(`--visibility public` only if the user chose it.) It creates the repo, closes its
issues, wiki, projects, discussions and outside pull requests, and points the
README's Open in Imbue Studio button at the new repo with a commit in `$WT`. It
prints `<owner>/<repo>`; keep it as `REPO`. On a public repo, also tell the user
the two things GitHub will not let you close: pull requests can only be limited
to collaborators, and a personal public repo can always be forked.

## 8. Push and tag

```bash
uv run --no-project --with 'pydantic>=2' python "$PT/push_snapshot.py" \
    --worktree "$WT" --parent "$BASE_REF" --repo "$REPO" \
    --message "template: <slug>

Assembled on clean DEFAULT_WORKSPACE_TEMPLATE base $BASE_REF (provenance link only; no upstream fetch)." \
  && python3 "$PT/github_repo.py" topic --repo "$REPO"
```

`push_snapshot.py` runs the pre-push gate and publishes ONE new commit holding
`$WT`'s final tree, parented on `BASE_REF`. So the repo's `main` is the
template's own history plus exactly one commit: it shares a merge-base with the
template and every template built on it, which is what lets an adopter merge it
in cleanly, and none of the worker's intermediate commits -- which can hold a
value a published-copy change removed -- ever leave the machine. It prints the
published commit; keep it as `PUBLISHED`. The topic is what makes templates
findable as a group; if only that call fails, the publish still stands --
mention it as a follow-up.

## 9. Record it, and check the published page

Only after the push succeeded:

```bash
python3 "$PT/record_publish.py" --workspace /home/user/workspace --slug <slug> \
    --repo-url "github.com/$REPO" --note "first published" --source-sha "$SOURCE_SHA"
```

It writes this publish into `docs/VERSION_HISTORY.md` and commits that one file.
If it reports a failed commit, the template IS published: say so, and commit the
file rather than re-pushing anything.

Then open the published README and check it renders, per "Verify the published
page" in `references/readme-recipe.md`. If something is wrong, fix it in `$WT`,
commit, and re-run step 8's `push_snapshot.py` command alone with
`--parent "$PUBLISHED"`, so the fix fast-forwards onto your own publish (with
`--parent "$BASE_REF"` it is refused as if someone else had pushed). Keep the
new commit it prints as `PUBLISHED` and check again. Record nothing more: the
version is already in the ledger.

## 10. Close out

Destroy the worker (this removes `$WT`) and its branch; the published commit lives
on GitHub, and the branch's intermediate commits were never meant to:

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py destroy --name <slug>
git worktree prune
git branch -D "mngr/<slug>"
```

If you are stopping after a failure instead, leave the worker, `$WT` and the
branch for a retry. Give the user the repo's URL in your final message.

## Publishing over an earlier template

Publishing from an agent that already has a `template.toml` replaces it rather
than adding a second. What carries over is the lineage: the assembly copies the
outgoing manifest's `[[lineage]]` entries, plus one for the outgoing manifest
itself when it has an `[origin]` (the template this agent adopted). So the new
manifest names every template it was built on, each with the repo and commit
where it can still be read. The superseded template's code stays in the tree;
only its manifest is replaced. A manifest this agent published itself, or any v1
manifest, has no `[origin]` and contributes no link -- add one by hand to the new
`template.toml` before the push if it matters.
