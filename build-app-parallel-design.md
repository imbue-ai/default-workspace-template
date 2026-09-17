# build-app-parallel: design

This design covers a new skill, `build-app-parallel`. A planning agent writes a plan, and the main agent carries it out by launching workers. Settled choices are in the "Decisions" section. Each problem lists the current solution and alternatives to try if it fails.

**Working copy:** `/Users/nayana/code/default-workspace-template-worktrees/build-app-parallel`, on the new branch `nayana/build-app-parallel` from `main` (`7c0487383`).

## How it works today

- **`build-app`** (`.agents/skills/build-app/SKILL.md`): the main agent does the whole build itself:
  1. Ask any blocking questions and propose a small plan.
  2. Build a throwaway mock and loop with the user until they confirm it.
  3. Build the real app.
  4. Loop with the user on the working site.
  5. Hand the app to `crystallize-creation` for hardening in the background.
- **Plan recorder** (`system/scripts/imbue_plan_extra/write_plan.sh`): `build-app` starts it and ignores it. It runs a headless, read-only `claude -p` using `prompts/build-app.md` and writes a DAG plan (a graph of tasks where each task lists the tasks it depends on) to `data/.imbue/plans/`.
  - The plan has three lists with one entry per node: `capability` (low / medium / high / interactive), `subtasks`, and `access list` (which earlier nodes a node waits on and reads).
  - Every plan starts with a "DO NOT USE" header.
  - A `CLAUDE.md` in the plans folder tells agents never to read plans.
- **`launch-task` workers** (`create_worker.py launch --template worker`): each worker gets its own git worktree on branch `mngr/<name>`, started from the main agent's last commit. At startup it runs `uv sync --all-packages`. A stop hook keeps it from ending a turn with uncommitted work.

## Implementation status

Built on `nayana/build-app-parallel` (commits `92a21942f` through `6d3c705d8`):

| Piece | Where |
|---|---|
| Orchestration skill the main agent follows | `.agents/skills/build-app-parallel/SKILL.md` |
| Planner instructions | `system/scripts/imbue_plan_extra/prompts/build-app-parallel.md`: a copy of the recorder's `build-app.md` prompt changed only where the new setup makes it untrue |
| Rules every shared-folder worker follows | `.agents/skills/build-app-parallel/references/worker-node.md` |
| Planner runner | `system/scripts/imbue_plan_extra/write_plan.sh --run-dir <dir> build-app-parallel` (new foreground mode; the background recorder mode is unchanged) |
| Plan check, ready-node list, task writer | `.agents/skills/build-app-parallel/scripts/plan_orchestration.py` (27 tests) |
| `--work-folder` and `--model` launcher options | `.agents/skills/launch-task/scripts/create_worker.py` (4 new tests) |
| Worker template | `[create_templates.shared_folder_worker]` in `.mngr/settings.toml` |
| Startup sync skipped for shared-folder workers | `.claude/settings.json` |
| New-app routing | `build-app` description, `do-something-new`, `fetch-process-show`, `manage-layout`, `update-app`, `crystallize-creation`, `CLAUDE.md` |

A real planner run on a to-do list brief produced a 7-node plan that passed the check: the spec and scaffold start together, the data layer runs during the mock review in files of its own, and one node owns the shared registration files. The first attempt at that run exited 1 with no output, and the rerun succeeded; the cause is unknown, and the recorder now keeps the planner's output in the run's `log` on failure. That run used an earlier, heavily rewritten prompt, since replaced.

Not yet run: a full build end to end in the workspace container.

## Proposed shape of `build-app-parallel`

1. **Clarify:** the main agent asks only blocking questions, as `build-app` Step 0 does.
2. **Plan:** the main agent briefs the planner and waits for the plan.
   - **Prompt:** the planner uses its own prompt, `prompts/build-app-parallel.md`.
   - **Storage:** plans go to a readable location with no "do not use" header.
   - **Check:** a small script checks the three lists and converts them to JSON. All three lists must have the same length, and access lists may point only at earlier nodes.
   - **Visibility:** the user never sees the plan.
3. **Set up the build folder:** the main agent creates one git worktree and branch for the whole build and runs `uv sync --all-packages` in it once (see problem 1).
4. **Orchestrate:** the main agent repeats these steps until every node is finished:
   1. Launch every node whose dependencies are done, up to 5 at a time. Each node is a worker running inside the build folder. Its task file carries its subtask plus the subtasks and reports of the nodes in its access list.
   2. Background-poll each worker for its report.
   3. Commit the build folder whenever no worker is running (see problem 4).
5. **Interactive nodes** (the mock review and the working-site review): the main agent runs these with the user, showing a preview served from the build folder.
6. **Finish:** the main agent merges the build branch into main, starts the app under supervisord, and runs `crystallize-creation` itself. That skill starts its own worker from main, so the app must be on main first. Handing it to a worker would put a worker inside a worker.

The build knowledge stays in `build-app`: scaffolder, ports, icons, verification, file paths. The planner and the workers read it there, and `build-app-parallel` holds only the orchestration procedure.

## Problems and solutions

### 1. Where the workers run

The planner prompt assumes one shared workspace: "whatever an earlier node wrote to disk is there for a later one to find." `launch-task` gives each worker its own worktree instead.

**Current solution: one shared build folder.** A single folder is possible. `mngr create --transfer=none --from :<path>` starts an agent inside an existing folder with no copy. The chat and caretaker agents already use `transfer = "none"`.
1. The main agent creates one worktree and branch for the build, e.g. `build/<app-name>`.
2. Every node worker is created inside that folder.
3. A node sees earlier nodes' files as soon as they are written, with no merge in between.
4. Gitignored files under `data/` are shared the same way.
5. There is one merge, into main, at the end.

Costs:
- **Collisions:** two workers editing the same file at the same time can overwrite each other. The plan must give parallel nodes separate files (problem 2).
- **Commits:** workers can't safely commit in a shared folder (problem 4).
- **Tested:** two agents ran in one folder at once and saw each other's files (see "Test results").
- **A failed worker start deletes the shared folder (mngr bug, found in testing).** When `mngr create` fails after it has resolved the work folder, its cleanup (`_cleanup_failed_worktree_create` in `system/vendor/mngr/libs/mngr/imbue/mngr/api/create.py`) runs `git worktree remove --force` on that folder. With `--transfer=none --from :<folder>`, the folder is the shared build folder, so one bad start (here, a Claude Code version mismatch) deletes every other worker's uncommitted work. The function's own docstring says a caller-provided folder must never be removed, so this is a bug. Options:
  1. **Fix mngr (recommended):** skip that cleanup when the transfer mode is `none`, and submit the fix upstream through the `submit-upstream-changes` skill.
  2. **Make the build folder a plain clone:** `git clone --local` gives a folder that isn't a worktree, and the cleanup does nothing for non-worktrees. The finish step then fetches the build branch from the clone before merging.
  3. **Commit more often:** the orchestrator commits before starting each batch of workers, so a deletion loses at most that batch's work. This only limits the damage.

**Alternatives to try:**
- **One worktree per worker** (what `launch-task` does today). Workers can't overwrite each other. The main agent must merge each finished branch before launching the nodes that depend on it, resolve conflicts at every merge, and copy `data/` files into each worker. Each worker also runs its own `uv sync`, which uses more memory.
- **Run in the main checkout.** The app is live the moment it's scaffolded, so no preview is needed. A rejected mock then sits as uncommitted work on main and has to be cleaned up, and every worker shares the folder the user's own chat works in.

### 2. Merge conflicts and file collisions

**Current solution: the plan avoids them.** The planner prompt gets these rules:
- **Separate files:** nodes that run at the same time own separate files, and each subtask names what it owns.
- **Shared files:** exactly one node touches the root `pyproject.toml`, `uv.lock` and `system/supervisord.conf`. That node scaffolds the app and adds all dependencies. The scaffolder also picks the port, so only one port is ever chosen.
- **What's left:** the main agent resolves any conflict in the final merge into main. That merge only conflicts if main changed the same files during the build, e.g. another app was added to `system/supervisord.conf`.

**Alternatives to try:**
- **Integration node:** nodes write their pieces as separate files, and a final serial node wires them into shared files (routes, templates).
- **Main agent resolves everything:** with one worktree per worker, the main agent merges after every node and resolves conflicts as they come. This is the fallback if plans can't reliably keep files separate.

### 3. Showing the user work that isn't on main

Apps run under supervisord from the main checkout (`directory=/home/user/workspace`), so an app in the build folder is invisible until it's merged.

**Current solution: preview from the build folder.**
1. The main agent serves the build folder on a spare port with `.agents/shared/scripts/serve_isolated_instance.py`, wrapped in a labeled "preview" tab. `update-system-interface` already does this for its pre-merge preview.
2. The user reviews the preview. Nothing lands on main until they confirm.
3. At the finish, the main agent stops the preview, merges the branch, runs `supervisorctl reread && supervisorctl update`, and opens the real tab.

**Alternatives to try:**
- **Merge before the working-site review.** Preview only the mock, then merge after the mock is confirmed and do the working-site review on the real tab. This matches `build-app`, where the working site is already live.
- **Merge first for both reviews.** It is simpler and shows the real tab, but a rejected mock has to be reverted on main.

### 4. Committing in a shared folder

Two workers committing in one folder race on git's lock file, and `git add -A` from one worker sweeps up another worker's half-finished files.

**Current solution: only the main agent commits.**
- **Workers:** never commit.
- **Main agent:** commits the build folder at points where no worker is running: before each interactive node and before the finish.
- **Stop hook:** the node template turns off the uncommitted-changes check (problem 6), since every worker would see other workers' uncommitted files.

**Alternatives to try:**
- **Workers commit their own files only**, with `git commit -- <paths>`. This gives per-node history, but the pre-commit hook reformats files and can touch paths a worker doesn't own.

### 5. Applying mock feedback

The planner prompt makes the review node the main agent's job, and the node that built the mock has already finished.

**Current solution: keep the mock worker running.** After its report the worker stays alive. The main agent sends it the user's feedback with `mngr message`, and it reports again after each round.

**Alternatives to try:**
- **Main agent edits the mock itself.** Fast for small changes, but it puts build work back on the main agent.
- **A new revision node per round**, given the original mock node's report plus the feedback. It starts cold, so each round is slower.

### 6. Review and testing cost

Each worker loads the repo's `CLAUDE.md`, which requires running every test suite with coverage before finishing. The `worker` template also keeps the stop hook on. With 8–10 nodes, reviewing each one would be very slow.

**Current solution: one review at the end.**
- **Node workers:** do a quick check that their own piece works (it serves, the page renders) and skip the full test suite, `/autofix` and the review gates.
- **The single review:** the `crystallize-creation` hardening pass at the finish runs the thorough tests and review once, for the whole app.

This needs a new mngr template for node workers, `[create_templates.shared_folder_worker]` in `.mngr/settings.toml`:

| Setting | `worker` template (today) | `shared_folder_worker` template |
|---|---|---|
| `transfer` | `git-worktree` | `none` (the orchestrator passes `--from :<build folder>`) |
| Setup commands | `uv sync --all-packages`, install `harden-worker` | none (the orchestrator syncs the build folder once) |
| Stop hook (`REVIEWER_STOP_HOOK_ENABLE`) | on | off |
| `MNGR_AGENT_ROLE` | `worker` | `shared_folder_worker` |
| Extra system prompt | "You were launched by another agent..." | You are one node of a plan: stay inside your subtask's files, don't commit, do a quick check only, and leave the full tests and review to the final hardening pass |

The skill for this is new, and so is the template: the node workers need different settings, not just different instructions. `CLAUDE.md` says in strong terms that all tests must run. The appended prompt may not override that, and `CLAUDE.md` may need an exception for the `shared_folder_worker` role.

**Alternatives to try:**
- **A review node before the working-site review**, so problems surface before the user tests the site. The final hardening pass still runs.
- **Quick checks as a single node:** workers do no checking at all, and one node verifies the assembled app.

### 7. Parallelism and resources

**Current solution: at most 5 workers at once.** The planner prompt says to allow up to 4–5 nodes in parallel. Workers share one Python environment in the build folder, so each extra worker adds a Claude process but no new environment.

Shared-folder workers don't sync at startup. The project's SessionStart hook in `.claude/settings.json` runs `uv sync --all-packages` in every Claude agent, and it now skips agents whose `MNGR_AGENT_ROLE` is `shared_folder_worker` (commit `af971ee9d`). The orchestrator syncs the build folder once when it creates it, and again after any worker that changes dependencies.

**Alternatives to try:**
- **Lower the cap to 3** if the out-of-memory process killer stops workers (the launcher's poll reports this as exit code 75).

### 8. Model per capability

`create_worker.py launch` has no model option.

**Current solution:**
- **Launcher option:** add `--model` to `create_worker.py launch`, passed through to mngr as `-S agent_types.claude.settings_overrides.model=<model>`. Tested: a node created with `model=sonnet` ran as `claude-sonnet-5`, and a node without the override ran as `claude-opus-5[1m]`.
- **One mapping table:** keep the capability-to-model mapping in a single table the orchestrator reads.
- **Default:** every capability maps to Opus.

**Alternatives to try:**
- **Split by capability:** low → Haiku 4.5, medium → Sonnet 5, high → Opus 5, once runs are stable enough to compare cost against results.

### 9. Waiting for the plan

A plan run takes minutes (the recorder allows up to 15), and the user waits.

**Current solution:** the main agent starts the planner in the background right after clarifying, and tells the user it's planning.

**Alternatives to try:**
- **A faster model for the planner**, if plan quality holds.
- **Start the mock early:** the main agent builds the mock while the plan is written. This makes the mock a fixed first stage outside the plan.

## Decisions

| # | Question | Decision |
|---|---|---|
| 1 | Should `build-app-parallel` replace `build-app` as the default on this branch? | Yes. Point `do-something-new` and `fetch-process-show` at `build-app-parallel`, and narrow `build-app`'s description so it no longer triggers on "build me an app". |
| 2 | Should the user see and approve the plan before any worker starts? | No. The user never sees the plan. The mock review is their first checkpoint. |
| 3 | Show work by merging first, or by previewing? | Preview (problem 3). Tentative: confirm once the preview has been tried on a real scaffolded app. |
| 4 | Which model does each capability get? | Opus for every capability to start. The mapping lives in one table and must be easy to change and test. |
| 5 | Rewrite `prompts/build-app.md`, or add a separate prompt? | Add `prompts/build-app-parallel.md`. The offline recorder and its prompt stay unchanged, so its plan data stays comparable to what's already collected. |
| 6 | How many nodes run at once? | Up to 4–5. |
| 7 | How are merge conflicts handled? | The plan is built to avoid them. The main agent resolves any that remain. |
| 8 | Does every worker run review? | No. One review, in the final hardening pass. |
| 9 | One shared build folder, or one worktree per worker? | Shared folder: building has started on it. A failed worker start currently deletes the folder (problem 1), and that needs a fix before real use. |

## Test results: `shared_folder_worker` template

The template is `[create_templates.shared_folder_worker]` in `.mngr/settings.toml` on `nayana/build-app-parallel`. The test below ran under its first name, `plan_node` (commit `d9081f5b9`), before the rename, with the same settings. Only the role value and the prompt's wording changed.

**Setup (on the Mac, outside the workspace container):**
1. **Build folder:** a throwaway git worktree at `/tmp/bap-build`, on branch `bap-test/build`.
2. **mngr:** the workspace's own mngr and its plugins, installed into `/tmp/bap-mngr-venv`. It ran with `MNGR_HOST_DIR=/tmp/bap-mngr-host` and `MNGR_PREFIX=bap-`, so it never touched the Mac's own mngr agents.
3. **Agents:** two created with `mngr create <name> -t shared_folder_worker --from :/tmp/bap-build --message "<task>"`.
   - `node-a` used the default model.
   - `node-b` added `-S agent_types.claude.settings_overrides.model=sonnet`.
   - Both added `-S agent_types.claude.version=2.1.273`, because the Mac's Claude Code version differs from the workspace pin (2.1.227).
4. **Tasks:** each node wrote its own file (`node_a/hello.py`, `node_b/hello.py`), ran a one-line check, and wrote a report into `bap-reports/`. `node-b` also waited for `node-a`'s report and quoted it.

**Results:**

| Check | Result |
|---|---|
| Both agents run inside the shared folder | Yes: `pwd` was `/tmp/bap-build` for both |
| No branch switch or new branch | Yes: the folder stayed on `bap-test/build` after both creates |
| `MNGR_AGENT_ROLE` | `shared_folder_worker` in both |
| Stop hook off | `.reviewer/settings.local.json` has `stop_hook.enabled_when = "false"` |
| Nodes see each other's files | Yes: `node-b` quoted the first line of `node-a`'s report |
| Nodes don't commit | Yes: no new commits, all work left untracked |
| Per-node model override | `node-a`: `claude-opus-5[1m]`; `node-b`: `claude-sonnet-5` |
| Nodes skip tests and review | Yes: `node-a` ran only the one-line check and said it skipped tests, commits and review as the prompt instructed |
| Time | Both reports were present within ~40 seconds of creating `node-b` |
| Destroying one node | `mngr destroy node-a --force` left the shared folder, its files and `node-b` running |
| Existing template tests | `system/test_mngr_template_stacking.py`: 5 passed |

**Follow-up test: skipping the startup sync.** After the hook change (problem 7):
- **Hook command:** run through `/bin/sh` with a fake `uv`. It skipped `uv` for role `shared_folder_worker` and called `uv sync --all-packages` for role `worker`, an empty role and no role.
- **Before the change:** the transcripts of `node-a` and `node-b` each show the hook running `uv sync --all-packages` (failing on the Mac because `pcmflux` has no macOS wheel).
- **After the change:** `node-c`, created from the updated template, started with no `uv sync` hook output.

**Follow-up test: a failed start.** The first `node-c` create failed on a Claude Code version mismatch, and the failure deleted `/tmp/bap-build` and unregistered it as a worktree (see problem 1). The folder was recreated before the hook test above.

**Findings to act on:**
- **Failed starts delete the shared folder:** an mngr bug (problem 1). It must be fixed or worked around before real use.
- **Stray `runtime/` folder:** a `runtime/oom_priority/agent_pids/` folder appeared in the build folder, most likely written by the OOM launcher. In the container, `OOM_PRIORITY_RUNTIME_DIR` points at an absolute path under `/home/user/workspace/data/`, so this may only happen on the Mac. Check in the container, because a stray folder would end up in the orchestrator's commits.
- **`tk` files:** `node-b` created step records, which landed in `.tickets/` (gitignored) in the build folder. In the container, `TICKETS_DIR` points at the shared workspace folder.
- **Old instructions still followed:** `node-a` said it skipped `CLAUDE.md`'s `tk` and README rules because the task said "exactly this and nothing else". A real task without that wording may follow them (problem 6).

**Not tested:**
- Running inside the workspace container
- Going through `create_worker.py`, which has no `--from` or `--model` options yet
- The orchestrator committing the build folder
- Five nodes at once
- Two nodes editing the same file
- Serving a preview from the build folder

## Notes

- **Not verified:** that `serve_isolated_instance.py` can serve a freshly scaffolded app from a build folder. I read its docs without running it.
- **Planner prompt edits:** four sentences in the current planner prompt become false or change meaning in the real flow:
  - "the workers share one workspace" (true again only with a shared build folder)
  - "whatever an earlier node wrote to disk is there for a later one to find" (same)
  - "nothing in this workspace reads it back"
  - "the final node is a worker"

  The new prompt also needs the file-ownership and parallelism rules from problems 2 and 7.
- **Progress timeline:** I plan to show each launched node as one entry with a plain-English title. With 8–10 nodes that makes a long list, and one entry per stage (plan, mock, build, review, finish) may read better.
