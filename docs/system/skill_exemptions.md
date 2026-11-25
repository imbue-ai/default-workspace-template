# Skill exemptions

An exemption records that a skill fails named checks of `validate_skill.py` and may, for now. The checks, and the rule that a failing check is fixed by restructuring the skill, are in `docs/system/skill_authoring_guide.md`. This file says how this repository records an exemption and who may write or clear one.

An exemption lives in the skill's own frontmatter, as two `metadata` strings:

```yaml
metadata:
  exempt-checks: size depth
  exempt-reason: awaiting-restructure
```

The checks are `size`, `reachability`, `depth`, `paths`, `contents` and `boundary`. There are two reasons:

| Reason | Meaning | Who writes it | Who clears it |
|---|---|---|---|
| `predates-checks` | The skill existed before the checks did. | Only a flow that brings existing skills under the checks: an update, a migration, adopting a template. Never by hand. | Whoever next edits the skill. |
| `awaiting-restructure` | A built-in skill whose restructure is scheduled. | Only the maintainers of this repository. | The same maintainers, in the release that restructures it. |

The rules around them:

- An edit to a skill that carries `predates-checks` clears it: fix what the exempted checks report, then remove the two lines. `validate_skill.py --edited` refuses that reason.
- An edit to a skill that carries `awaiting-restructure` leaves the exemption, and the skill's structure, as they are. Restructuring it in a copy this repository does not own only creates a divergence the real restructure has to reconcile. The edit must not fail any further check.
- A new skill carries none: `validate_skill.py --no-exemptions` refuses any.
- An exemption naming a check the skill now passes is stale, and stale is an error.
- `validate_skill.py --stamp` is how a flow writes one. It records what each existing skill fails, gives a skill it newly exempts the reason `predates-checks`, and removes an exemption that has gone stale.
