# skills-catalog

The skills catalog: every skill the workspace's agents have, read fresh from
`.agents/skills/` on every call. That one folder is what every harness in the
workspace reads (claude through the `.claude/skills` symlink; codex, pi,
opencode and antigravity natively), so a skill made by `crystallize-creation`,
changed by `update-creation`, brought in by `use-template` or `update-self`, or
installed with `npx skills add` shows up the moment it is on disk.

`SkillsCatalog.list_skills()` answers one `SkillSummary` per directory holding a
`SKILL.md`, and the skills folder's absolute path; `describe_skill(name)` adds
the skill's own folder, the file exactly as written and the files beside it. A `SKILL.md` is parsed with mngr's own reader
(`imbue.mngr.utils.agent_skills`), which is as lenient as the agents' loaders,
and checked against the agentskills.io layout with its `skill_shape_problems`.

Each skill's `source` says who it comes from, and is what the Skills app groups
by: `STUDIO` when its frontmatter's `metadata.author` is `imbue` (every skill the
template ships carries it), else `INSTALLED` when a skills lock file lists it,
else `WORKSPACE`. Its `audience` is `SYSTEM` when its `metadata.audience` is
`system` (the agents' own procedure, which the app keeps behind the scenes),
else `PEOPLE`. Its `last_updated_at` is its newest commit's time, or, while
its folder has unsaved changes, its newest file's.

Each skill's `origin` comes from the workspace's git history and the skills
lock files:

| Origin | Meaning |
| --- | --- |
| `BUILT_IN` | Added by the template and last changed by it. |
| `CHANGED_HERE` | Added by the template, then changed in this workspace. |
| `INSTALLED` | Added in this workspace and recorded in a skills lock file. |
| `MADE_HERE` | Added in this workspace. |
| `NOT_SAVED` | On disk, but no commit has saved it. |

A commit is the template's when it is the root commit or lies on the upstream
side (the second parent) of an `update-self:` merge -- the same test the
`assist` skill applies, narrowed so that the workspace's own commits before a
merge are not counted as the template's. The history is read with three or four
git commands and kept until HEAD moves.

The lock files are `.agents/skills-lock.json` (where the template keeps its own)
and `skills-lock.json` (where `npx skills add` writes a new one at the
checkout's root); the first that lists a skill names its source.

`SKILLS_CATALOG_REPO_ROOT` names another checkout; by default it is the current
directory. The Skills app (`system/apps/skills`) is the catalog's one user.
