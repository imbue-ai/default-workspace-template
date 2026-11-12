- `system/test_skills.py` (new) runs `validate_skill.py`'s checks over every skill that `.agents/third-party-skills.toml` does not list, and fails when a file under `.agents/shared/` is reached from nothing.

- `docs/system/skill_authoring_guide.md` is the skill authoring guide, fetched from the pinned mngr commit by `system/scripts/fetch_mngr_assets.sh` like the style guide.

- `AGENTS.md` tells an agent that edits a skill to run `validate_skill.py --edited` on it (`--no-exemptions` for a new skill), and to leave a built-in skill's `awaiting-restructure` exemption, and that skill's structure, alone.

- `.reviewer/code-issue-categories.md` gains a `skill_hygiene` category, which reviews skill markdown against the guide for what the validator cannot check.

- The mngr pin moves to a commit that carries the guide.
