- `system/test_skills.py` fails when a file under `.agents/shared/`, or `AGENTS.md`, links a file inside a skill other than its `SKILL.md`: either may name a skill, and what several skills need lives in the shared layer.

- `AGENTS.md` names the shared `review-gates.md` for a shed command of a worker's test gate, and the `submit-upstream-changes` skill for a change to mngr.

- `user_packages.py` and `system/test_python_launch_rules.py` name `python-packaging-migration.md` at its shared path, `.agents/shared/references/`.

- `docs/system/skill_exemptions.md` lists `boundary` among the checks.
