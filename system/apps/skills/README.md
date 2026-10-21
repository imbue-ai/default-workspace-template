# skills (the app)

The Skills app: one window over every skill this workspace's agents have. It is
the user's view of the skills catalog (`system/libs/skills_catalog`), which
reads `.agents/skills/` fresh on every request -- the one folder claude (through
the `.claude/skills` symlink), codex, pi, opencode and antigravity all read --
so a skill an agent made, changed or installed a moment ago is listed on the
next load.

- **`/`**, the list: one row per skill (its name, where it came from, its
  description), with a search field and a filter per origin: built in, built in
  and changed here, installed (recorded in a skills lock file, e.g. by
  `npx skills add`), made here, and not saved yet. A row also says when the
  skill has unsaved changes or its `SKILL.md` departs from the agentskills.io
  layout.
- **`/<name>`**, a skill: where it came from, when it was last saved, what lock
  file records its install source, its layout problems, the files in its
  folder, and its `SKILL.md` exactly as written.

The app is read-only and stores nothing. Skill text is shown as text, never as
markup, since a skill installed from elsewhere is untrusted content.

| Route | Answers |
| --- | --- |
| `GET /api/skills` | The listing (`SkillListing`). |
| `GET /api/skills/<name>` | One skill (`SkillDetail`); 404 for a name that is not a skill. |
| `GET /api/health` | Whether the app is up. |

Either listing route answers 500 with the reason when the workspace's git
history cannot be read. The API is typed through `app_schema` and published at
`/api/schema`; every model is the catalog's.

The page is one hand-written document (`src/skills_app/static/index.html`, no
build step) that reads its path, like the System Monitor app's. It serves on port
8060 by the `skills` program; `SKILLS_APP_PORT` and `SKILLS_APP_HOST` are how a
preview boots.
