Skills are now held to the skill authoring guide (`docs/system/skill_authoring_guide.md`).

- **`validate_skill.py` checks five more things.** `size` (SKILL.md at most 14,000 bytes, the 5,000 tokens an agent keeps of a skill after a compaction, and 500 body lines), `reachability` (every file in the skill's directory is linked), `depth` (no reference more than two links from SKILL.md), `paths` (every path named under `.agents/`, `system/` or `docs/` exists) and `contents` (a long file has a list naming its sections; a numbered entry `3. Make the change` names `## Step 3: Make the change`). A failure states the goal and says restructuring is permitted.

- **Exemptions.** A skill that fails a check carries `exempt-checks` and `exempt-reason` in its frontmatter `metadata`. 19 built-in skills carry one, with the reason `awaiting-restructure`. A stale exemption fails. `--no-exemptions` refuses one, and a new skill is validated that way (`op-crystallize.md`). `--edited` is the mode for a skill that was edited (`type-skill.md`): it refuses `predates-checks`, the reason a user's own skill carries, so the edit clears it; it honours `awaiting-restructure`, so nobody is asked to restructure a built-in skill locally ahead of the release that does.

- **`validate_skill.py --stamp`** records what each existing skill fails as an exemption and changes nothing else. A skill it newly exempts gets `predates-checks`; one that already carries an exemption keeps its reason. `update-self`'s worker, `migrate-workspace`, `use-template` and `update-installed-template` run it, and so does the packaging migration (`python-packaging-migration.md`) when a skill it converts fails a check, so a user's existing skills are never restructured by a flow.

- **`imbue-studio` is listed as a third-party skill**, so the checks leave it out; it is restructured in the mngr repository.

- **Contents lists** were added to 28 long SKILL.md and reference files, and `publish-template`'s checklist names its last section. The Caretaker's welcome message is a fenced block, so its headings are no longer read as sections of the skill.

- **`spec-summary.md`** now holds only this workspace's packaging rules; the rest moved to the guide. The product terms skills use are in a new shared `glossary.md`, which `AGENTS.md` names.

- **Shared layer.** The worker's reference table names its operation and type files, the orphaned `verification.md` is deleted, and example paths in `service-processes.md` and `running-python.md` use placeholders.

- **`harden-worker` is a skill.** It moved from `.agents/shared/worker/` to `.agents/skills/harden-worker/`, with its references. Its description says it is not for a chat to invoke. The task files `crystallize-creation`, `heal-creation` and `update-creation` write tell the worker to invoke the `harden-worker` skill. Every pointer to the old path follows.

- **Deleted.** The `dealing-with-the-unexpected` skill; the Co-Authored-By trailer in `submit-upstream-changes`; the pointer to `submit-upstream-changes` in `launch-task`'s dead-worker reference, which now says to merge the branch; the review stages `lead-proxy.md` named that no longer run.

- **Small corrections.** `notify-user`'s `compatibility` line matches how it runs. `do-something-new` no longer names a skill to the user. `connect-external-service` states the secret-card procedure once, in `direct-api.md`, and drops its hard-coded service list in favour of `latchkey services list`. Stale citations and terms are fixed in `launch-task` and `check-app-errors`.

- **`validate_skill.py`.** A shared file's relative path is no longer resolved against the skill that reads it, and a linked script that imports the skill's package reaches the skill's `python/` project.

A skill's files have one reader, an agent following that skill: nothing outside a skill links a file inside it other than its `SKILL.md`. Knowledge two skills need lives in the shared layer, written for any reader. A flow that should take over is reached by naming the skill. What another party does is said in the skill's own terms, without a link.

- **`validate_skill.py` has a sixth check, `boundary`**: a skill's markdown links no markdown file inside another skill, other than that skill's `SKILL.md` (another skill's scripts may still be run). `skill_checks.py` also gains `outside_links_into_skills`, which lists every such link from `.agents/shared/` and `AGENTS.md`.

- **`review-gates.md`** (new, under `.agents/shared/references/`) is how a worker runs its test gate, and what it does about a failing test, a test it would change, a suite the selector left out, a regression the gate let through and a command that dies from a signal. It was the "Review gates" section of the harden worker's `harden-creation.md`, which the update worker and `AGENTS.md` read too. `harden-creation.md` keeps what is a harden pass's own (selecting the gate from the scope file) and links it.

- **`web-frontend-testing.md`** moves from `harden-worker/references/` to `.agents/shared/references/`, since the update worker reads it as well. `update-self` now reads nothing inside `harden-worker`.

- **`python-packaging-migration.md`** moves from `update-self/references/` to `.agents/shared/references/`: `migrate-workspace`, `use-template` and `update-installed-template` read it too. It gains a contents list, and tells a reader with no lead to take its questions to the user.

- **`worker-failure.md` and `dead-worker-recovery.md`** move from `launch-task/references/` to `.agents/shared/references/lead-proxy/`. Every lead follows them through `lead-proxy.md`, whichever skill it came from, so `dead-worker-recovery.md`'s commands take the lead's own task file rather than `launch-task`'s, and `worker-failure.md` no longer links the more specific `harden-contention.md`.

- **`apply-exit-codes.md`** (new, under `.agents/shared/references/`) says what each exit of `update_self.py apply` means for anyone who runs it, what its closing stderr lines ask for, how an interrupted apply resumes or heals itself, and what the phase timings line is. `update-self` and `update-app`'s careful flow both run that command, and both now read this file: `update-self`'s `apply-outcomes.md` keeps only what its own pass does on each outcome, each exit's section opening with the part of this file to read first, and the careful flow no longer names a file inside `update-self`.

- **Pointers that now name a skill.** A latchkey permission request in `github-sync`, `migrate-workspace`, `update-installed-template`, `update-published-template`, `use-template` and `datalib` names the `connect-external-service` skill, as does the visiting agent's primer. `build-app` and `interactive-delivery.md` name the `harden-worker` skill, and `freeing-memory.md` the `update-self` skill. A skill is named by its name alone, without the path to its `SKILL.md`.

- **`harden-worker` names each shared reference by its full path**, in its `SKILL.md` table and in the references that had only the bare filename, so the checks see those links.

- **Pointers dropped, with the fact stated where it is used**: `build-app`'s `.nobackup` rule, `update-creation` on a worker splitting its pass and on the critical-app handoff, `harden-contention.md` on how a worker catches up, and `freeing-memory.md` on the files that send a lead to it.

- **`spec-summary.md`** no longer links `user-facing-language.md`, which `AGENTS.md` names, so a skill that links the migration reference reaches nothing three links away.

- **Exemptions.** `update-app` gains `boundary`: it links `build-app`'s references, which its own restructure settles. `update-self` loses `depth`.

`update-self` reads the prose it follows outside its own directory from the release being updated to, not from the workspace's older tree.

- **`update_self.py stage-shared-prose --ref <ref>`** (new) extracts `.agents/shared/` from the target release into `data/.tasks/update-self/skill-at-target/`, beside the staged skill. When the ref predates the skill it stages the workspace's own copies, as `bootstrap-skill` does for the skill. It refuses, staging nothing, when a path is missing.

- **The lead runs it in Step 3a**, after the ceiling re-check settles the target. From Step 3 on, SKILL.md and every reference name those files by their staged path, and say to read a further `.agents/...` path a staged file names from the staged copy when one is there. The worker guide says the same. Steps 1-2 run before anything is staged and name the workspace's copies.

- **A test holds the prose to this**: every markdown path outside the skill's directory, from Step 3 on, uses the staged prefix and names a file `stage-shared-prose` stages.

- **`validate_skill.py`** counts a path into a copy of the tree (`<somewhere>/.agents/...`) as a link to the file it copies, so the depth check sees through the staged prefix.

The scripts the skill mirrors into its own package are unchanged.

- **`build-app` is a checklist.** SKILL.md is 13.9 KB, from 52.0 KB: its standing instructions, then a numbered list of seven steps (clarify and plan; choose the path, the name and the icon; scaffold; confirm a mock; build the real app; verify, then show; hand off to the harden pass), each naming the reference that carries its detail. The detail moved to new references: `scaffold-output.md` (the scaffolder's flags, the files it writes, the program block, how the app starts), `wrap-existing-server.md` (its own numbered steps) and `forward-port-cli.md`. A canonical run reads 28 KB, from 63 KB.

- **One statement each in `build-app`.** The naming rule, the bind host, port picking, the verify recipe, the public URL and the icon rules are each stated once; the icon's drawing rules are `docs/system/app-icons.md`'s alone. A port chosen by hand follows the scaffolder's own check (at or above 8080, free, named by no supervisord config and no registered app, and not 8083) in place of a list of the built-in apps' ports. The naming rule says what the scaffolder and `forward_port.py` enforce: a leading letter, at most 32 characters, `localhost`, `auth` and `github` reserved; it no longer lists `browser`, which nothing refuses. The app's origin is written with its `<name>-<random>` label, in `public-url.md` only.

- **`update-app` routes first.** SKILL.md is 13.4 KB, from 31.2 KB. A critical app is routed to `references/critical-app.md` in a section at the top, before anything else. Removing an app or service is routed there too. The ordinary change is a numbered list of six steps in the order the "verify before the user can see it" rule asks for: size the change, take the editing lease, make the change, apply and verify it, show it, harden it. The data-copy guide is `references/verify-against-a-copy.md`, and step 4 sends an agent who wants to try a change in a browser before the user sees it to that guide's throwaway instance, never a hand-made copy of the store or a hand-started app. The less common ways to apply a change are `references/applying-a-change.md`, and `critical-app.md` has a numbered list of its four steps and gives a full path to each file it sends the reader to. The careful flow's routing names the two shared references its lead follows, `lead-proxy.md` and `harden-contention.md`. A critical change reads 48 KB, from 59 KB, the rules for an app's code included.

- **What both skills need is in the shared layer.** `.agents/shared/references/apps/` (new) holds `code.md` (what the scaffolded starter sets up and the rules an app's code keeps: state under `DATA_DIR`, the shell page script, the content-security policy, raw data and its source), `manifest.md` (`stop_when_no_windows`, `[[references]]`, a pinned window, renaming an app), `api-schema.md` (the API document's route-table contract), `mock-frame.md` (the mock frame's commands), `verify.md` (the verify recipe) and `gotchas.md` (symptoms in the window), each written for any reader. `build-app` and `update-app` both link them, and neither links a file inside the other. `update-app`'s step 3 opens by sending an agent changing an app's code to `code.md`, which only `build-app` read before, and its careful flow names that read itself.

- **`build-app` asks for approval once before it builds, at the mock.** Step 1 ends the turn only for a question that blocks. With none, the agent goes on to the scaffold and the mock in the same turn, where it used to propose a plan and wait.

- **Neither skill reads `interactive-delivery.md` any more.** `build-app`'s standing instructions and steps state the loop themselves: confirm a cheap mock, build nothing real before that, harden in the background. Two rules that lived only in that file are now `build-app`'s own: each further piece of the task is its own delivery, and the agent does not drive the mock in a browser before showing it. `update-app`'s step for a larger change states its preview loop in its own words.

- **Descriptions.** Both follow the authoring guide's shape: what the skill does, when to use it and which skill takes the neighbouring case, with no steps. `build-app`'s names a request to see a mockup, sketch or design of an app before it is built.

- **Removing an app is `update-app`'s.** The teardown steps are its `references/removing-an-app.md`, beside the rule to leave the app's data behind; `build-app`, `forward-port-cli.md` and `service-processes.md` name the skill.

- **No link into another skill's files.** The careful flow reads what the apply's exit codes mean, and what to do about an interrupted apply, from the shared `apply-exit-codes.md` in place of a pointer into `update-self`; `critical-app.md` keeps only what the flow adds for each. It says what the harden worker does in its own words. `code.md` states the `.nobackup` rule without sending the reader to the harden worker's contract. The scaffolder's closing line names the shared `verify.md`.

- **A read every run needs is an instruction at its step**, as the authoring guide now asks. `build-app` reads `mock-frame.md` where the frame comes down; `update-app` reads `applying-a-change.md` and the copy guide's preview section where each applies; the careful flow reads `worker-failure.md` and `harden-contention.md` where it used to cite them.

- **After a compaction.** `AGENTS.md` carries the general rule, so `build-app` adds nothing to it, and `update-app`'s standing instructions say only that a careful change re-reads `critical-app.md` whole.

- **Terms.** Both skills say "app" for what the user opens and "service" for a program with no window; `update-app`'s prototype is a "preview" throughout.

- **Exemptions.** `build-app` carries none. `update-app` is no longer exempt from `size` or `paths`, and keeps `contents` only through shared references that other skills own.

- `fetch-process-show` points at `build-app`'s step 5 for taking the mock frame down.
