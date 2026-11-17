The lifecycle skills, the harden worker and the shared references they rest on follow the skill authoring guide, and none of `crystallize-creation`, `update-creation`, `heal-creation`, `harden-worker`, `launch-task` and `fetch-process-show` carries an exemption.

- **`harden-lead.md`** (new, under `.agents/shared/references/`) holds the dispatch steps the three lifecycle skills each spelled out: names and paths, the single-flight check, what the ticket's title and description are for, the task-file fields, the launch rules, polling, the checks before a merge, and the go-live by creation type.

- **`crystallize-creation`, `update-creation`, `heal-creation`** are checklists: standing instructions, what to settle before starting, a numbered step list, each step's own commands and task-file template, and the shared section the step needs. `update-creation`'s critical-app carve-outs moved to `references/critical-app-handoff.md`, which only a lead sent by `update-app`'s careful flow reads. Step numbers and the sections other skills cite are unchanged.

- **`lead-proxy.md` and `harden-contention.md`** each split into a core that keeps its path and holds what every reader needs, and a directory of the same name with one file for each path only some take: `lead-proxy/liveness.md`, `lead-proxy/milestones.md`, `lead-proxy/intermediate-lead.md`, `lead-proxy/rsync.md`, `harden-contention/catch-up.md`, `harden-contention/takeover.md`, `harden-contention/supersede.md`. The core links each one where its condition arises, and the sections that only handed off are gone.

- **The lead-proxy flow names no harden file.** The two checks every lead runs before merging a worker's branch are now `lead-proxy.md`'s "Before a merge": wait out an editing lease, and abort a conflicted merge when the lead's skill has a catch-up rule. `harden-contention.md` keeps the freshness check and everything else particular to a harden pass, and it and `harden-lead.md` link `lead-proxy.md` by path. `launch-task`'s `worker-failure.md` no longer points into the harden files; `harden-lead.md` says what becomes of a stopped harden pass.

- **The harden worker.** `harden-creation.md` keeps the contract every pass follows; the data rules, the test gate's failure cases, splitting the pass, carried work and catching up moved to `data-and-storage.md`, `test-gate-cases.md`, `splitting-the-pass.md`, `carried-work.md` and `catching-up.md`. `harden-worker/SKILL.md` opens with its standing instructions and step list, which has a step for doing the pass, and its tables list every one of those files with the condition that loads it. "Critical-app handoff", "Critical-app exception" and "A regression the gate let through" are headings.

- **After a compaction**, what to re-read is a standing instruction of each skill that leads or is a worker, `launch-task` included, and no longer a line inside `lead-proxy.md` or `harden-creation.md`, which a compaction drops.

- **Checking whether a worker is alive** uses `mngr capture <worker> --full --no-start`, which reads the agent's pane on any harness and does not start a stopped one.

- **`launch-task`** has a numbered step list, with its guidelines above it. **`fetch-process-show`** is under the 14,000-byte cap. **`interactive-delivery.md`** has a contents list and a shorter opening, and links nothing.

- **Two rules made exact.** `crystallize-creation` says what to do with the freshness check while the skill it is building is not on the lead's branch yet (every merge before the first that brings it): skip it, since `pass-scope` refuses a creation that does not exist and nothing could have moved under it. `lead-proxy.md` has a lead close its ticket where its skill says to, which for the three lifecycle skills is after go-live, as they always did; it used to say before.

- **Wording.** A worker's own workers are called that, not sub-workers or siblings; a service is no longer said to have a tab; stale lines in `post-crystallize-migration.md` (a PR description, a missing `tk` list command) are corrected.

The exemptions of `do-something-new`, `migrate-workspace` and `publish-template` lose the checks these shared files were the only cause of.
