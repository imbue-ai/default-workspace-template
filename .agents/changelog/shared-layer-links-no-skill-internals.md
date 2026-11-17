A skill's files have one reader, an agent following that skill: nothing outside a skill links a file inside it other than its `SKILL.md`. Knowledge two skills need lives in the shared layer, written for any reader. A flow that should take over is reached by naming the skill. What another party does is said in the skill's own terms, without a link.

- **`validate_skill.py` has a sixth check, `boundary`**: a skill's markdown links no file inside another skill, other than that skill's `SKILL.md`. `skill_checks.py` also gains `outside_links_into_skills`, which lists every such link from `.agents/shared/` and `AGENTS.md`.

- **`review-gates.md`** (new, under `.agents/shared/references/`) is how a worker runs its test gate, and what it does about a failing test, a test it would change, a suite the selector left out, a regression the gate let through and a command that dies from a signal. It was the "Review gates" section of the harden worker's `harden-creation.md`, which the update worker and `AGENTS.md` read too. `harden-creation.md` keeps what is a harden pass's own (selecting the gate from the scope file) and links it.

- **`web-frontend-testing.md`** moves from `harden-worker/references/` to `.agents/shared/references/`, since the update worker reads it as well. `update-self` now reads nothing inside `harden-worker`.

- **`python-packaging-migration.md`** moves from `update-self/references/` to `.agents/shared/references/`: `migrate-workspace`, `use-template` and `update-installed-template` read it too. It gains a contents list, and tells a reader with no lead to take its questions to the user.

- **`worker-failure.md` and `dead-worker-recovery.md`** move from `launch-task/references/` to `.agents/shared/references/lead-proxy/`. Every lead follows them through `lead-proxy.md`, whichever skill it came from.

- **Pointers that now name a skill.** A latchkey permission request in `github-sync`, `migrate-workspace`, `update-installed-template`, `update-published-template`, `use-template` and `datalib` names the `connect-external-service` skill, as does the visiting agent's primer. `build-app` and `interactive-delivery.md` name the `harden-worker` skill, and `freeing-memory.md` the `update-self` skill.

- **Pointers dropped, with the fact stated where it is used**: `build-app`'s `.nobackup` rule, `update-creation` on a worker splitting its pass and on the critical-app handoff, and `harden-contention.md` on how a worker catches up.

- **`spec-summary.md`** no longer links `user-facing-language.md`, which `AGENTS.md` names, so a skill that links the migration reference reaches nothing three links away.

- **Exemptions.** `update-app` gains `boundary`: it links `build-app`'s references, which its own restructure settles. `update-self` loses `depth`.
