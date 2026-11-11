Skills are now held to the skill authoring guide (`docs/system/skill_authoring_guide.md`).

- **`validate_skill.py` checks five more things.** `size` (SKILL.md at most 14,000 bytes, the 5,000 tokens an agent keeps of a skill after a compaction, and 500 body lines), `reachability` (every file in the skill's directory is linked), `depth` (no reference more than two links from SKILL.md), `paths` (every path named under `.agents/`, `system/` or `docs/` exists) and `contents` (a long file has a list naming its sections). A failure states the goal and says restructuring is permitted.

- **Exemptions.** A skill that fails a check carries `exempt-checks` and `exempt-reason` in its frontmatter `metadata`. 18 built-in skills carry one, with the reason `awaiting-restructure`. A stale exemption fails. `--no-exemptions` refuses one; a new skill and an edited skill are validated that way (`op-crystallize.md`, `type-skill.md`).

- **`validate_skill.py --stamp`** records what each existing skill fails as an exemption and changes nothing else. `update-self`'s worker, `migrate-workspace`, `use-template` and `update-installed-template` run it, so a user's existing skills are never restructured by a flow.

- **`imbue-studio` is listed as a third-party skill**, so the checks leave it out; it is restructured in the mngr repository.

- **Contents lists** were added to 28 long SKILL.md and reference files, and `publish-template`'s checklist names its last section.

- **`spec-summary.md`** now holds only this workspace's packaging rules and its glossary of product terms; the rest moved to the guide.

- **Shared layer.** The worker's reference table names its operation and type files, the orphaned `verification.md` is deleted, and example paths in `service-processes.md` and `running-python.md` use placeholders.
