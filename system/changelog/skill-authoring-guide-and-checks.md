- Adds three documents under `docs/system/blueprint/skills-best-practices/`.

  `report-skills-best-practices-audit.md` audits the 41 built-in skills and the `.agents/shared/` layer against the published agent-skill guidance (Anthropic best practices, the Claude Academy course, agentskills.io, Claude Code docs, and community sources). It catalogs, per skill and by kind, where the tree falls short: five SKILL.md bodies over the 500-line limit that the repo's own `validate_skill.py` rejects but no test runs against built-ins, 13 SKILL.md files over the 5,000 tokens Claude Code keeps of a skill after a compaction (measured with `count_tokens`), SKILL.md files written as full manuals, reference chains up to eight hops deep with cycles, 27 reference files over 100 lines with no table of contents, orphaned references, duplicated procedures across sibling skills, second-person and instruction-laden descriptions, dead and version-bound content, and deterministic procedures left as prose.

  `plan-skills-best-practices.md` is the fix plan: one authoring guide enforced by a validator and a review category, per-skill exemptions that later releases remove, and the first release's restructures in file-level detail.

  `rationale-skills-best-practices.md` says where each rule in the guide comes from: the published source it rests on, with links, or the decision that set it where no source did.

- `system/test_skills.py` (new) runs `validate_skill.py`'s checks over every skill that `.agents/third-party-skills.toml` does not list, and fails when a file under `.agents/shared/` is reached from nothing.

- `docs/system/skill_authoring_guide.md` is the skill authoring guide, fetched from the pinned mngr commit by `system/scripts/fetch_mngr_assets.sh` like the style guide. `docs/system/skill_exemptions.md` (new) says how this repository records that a skill fails named checks for now, the two reasons, and who may write or clear each.

- `AGENTS.md` tells an agent that edits a skill to run `validate_skill.py --edited` on it (`--no-exemptions` for a new skill), and to leave a built-in skill's `awaiting-restructure` exemption, and that skill's structure, alone.

- `AGENTS.md` also tells an agent what to re-read after a compaction: the reference its current step names, and the skill's `SKILL.md` first unless its whole text is still in context. The rule is stated there once, near the top, where every harness tested still has it after a compaction, and not in each skill.

- `.reviewer/code-issue-categories.md` gains a `skill_hygiene` category, which reviews skill markdown against the guide for what the validator cannot check.

- The mngr pin moves to a commit that carries the guide.

- `.reviewer/settings.json` sets code-guardian's `stop_hook.reviewable_markdown_paths` (from code-guardian 0.7.0) to `.agents/skills` and `.agents/shared`, so a branch that changes only skill markdown gets autofix and the other review gates, including `skill_hygiene`. Before, the Stop hook skipped any branch whose changes were all markdown.
