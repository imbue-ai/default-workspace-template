- The `update-self` flow now bootstraps itself from the version being updated to.
  After the lead resolves the target ref (still with the local resolver), a new
  Step 2a stages that ref's *own* copy of the update-self skill (SKILL.md,
  references, scripts) at a fixed path, and the rest of the pass -- lead *and*
  worker -- runs from the staged copy. So a fix to the conflict-triage,
  validation, or reveal logic that shipped in the target release is applied on the
  way *in*, instead of staying a release behind in the local copy.

- Added a `bootstrap-skill` subcommand to
  `.agents/skills/update-self/scripts/update_self.py`: it `git archive`s the skill
  dir at the resolved ref into a fixed staging dir under `runtime/update-self/`
  (already-fetched objects, no network, no working-tree mutation) and reports
  whether it differs from the local working-tree copy. The compare is a
  `git diff --quiet <ref> -- <skill dir>`, which ignores untracked files, so a
  build artifact like `__pycache__/*.pyc` never registers as a spurious
  difference. A ref that predates the skill stages the *local* copy at the same
  path instead, so the staged path always holds a runnable flow while the caller
  cleanly stays on the local flow.

- The staged flow lives at one fixed, literal path --
  `runtime/update-self/skill-at-target/.agents/skills/update-self` -- which the
  lead and worker both address directly. Because it sits under the runtime dir
  synced into the worker's worktree, the worker runs every `update_self.py` call
  from it without any value being carried across shell invocations (Claude's bash
  invocations don't share state, so the earlier env-var approach could silently
  fall back to the stale local copy).

- Hardened the reveal step that lands a `service` / `supervisord.conf` /
  `bootstrap` change: it now prescribes a full services-agent restart
  (`mngr start --restart system-services`) and explicitly forbids the surgical
  `supervisorctl reread && update`. The surgical reload restarts individual
  programs but does not re-run `bootstrap`, so it silently misses `libs/bootstrap`
  changes shipped in the target release; the old "supervisord reloads every
  program" gloss had been steering lead agents toward that wrong path. A
  breadcrumb in the `update-service` skill keeps its own legitimate use of the
  surgical reload from bleeding back into update-self.
