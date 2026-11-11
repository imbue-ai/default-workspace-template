The lifecycle skills, the harden worker and the shared references they rest on follow the skill authoring guide, and none of `crystallize-creation`, `update-creation`, `heal-creation` and `harden-worker` carries an exemption.

- **`harden-lead.md`** (new, under `.agents/shared/references/`) holds the dispatch steps the three lifecycle skills each spelled out: names and paths, the single-flight check, what the ticket's title and description are for, the task-file fields, the launch rules, polling, the checks before a merge, and the go-live by creation type.

- **`crystallize-creation`, `update-creation`, `heal-creation`** are checklists: standing instructions, a step list, each step's own commands and task-file template, and the shared section the step needs. `update-creation`'s critical-app carve-outs moved to `references/critical-app-handoff.md`, which only a lead sent by `update-app`'s careful flow reads. Step numbers and the sections other skills cite are unchanged.

- **`lead-proxy.md` and `harden-contention.md`** each split into a core that keeps its path and its section names, and rare-path files beside it: `lead-proxy-liveness.md`, `lead-proxy-milestones.md`, `lead-proxy-intermediate-lead.md`, `lead-proxy-rsync.md`, `harden-contention-catch-up.md`, `harden-contention-takeover.md`, `harden-contention-supersede.md`. Each core opens with a contents list saying when each section applies. Neither core links the other by path, so a skill that names one reaches nothing more than two links away.

- **The harden worker.** `harden-creation.md` keeps the contract every pass follows and its section names; the data rules, the test gate's failure cases, splitting the pass, carried work and catching up moved to `data-and-storage.md`, `test-gate-cases.md`, `splitting-the-pass.md`, `carried-work.md` and `catching-up.md`. `harden-worker/SKILL.md` opens with its standing instructions and step list, and its table lists every one of those files with the condition that loads it. "Critical-app handoff", "Critical-app exception" and "A regression the gate let through" are headings.

- **`interactive-delivery.md`** has a contents list and a shorter opening, and links nothing.

- **Two rules made exact.** The freshness check says what to do with a creation that is not on the lead's branch yet (the skill a crystallize pass is building, before the first merge that brings it): leave it out, since `pass-scope` refuses it and nothing could have moved under it. `lead-proxy.md` has a lead close its ticket where its skill says to, which for the three lifecycle skills is after go-live, as they always did; it used to say before.

- **Wording.** A worker's own workers are called that, not sub-workers or siblings; a service is no longer said to have a tab; stale lines in `post-crystallize-migration.md` (a PR description, a missing `tk` list command) are corrected.

`launch-task`'s `worker-failure.md` points at `harden-contention-supersede.md`. The exemptions of `launch-task`, `do-something-new`, `fetch-process-show`, `migrate-workspace`, `publish-template` and `update-app` lose the checks these shared files were the only cause of.
