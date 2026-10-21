# skills (the app)

The Skills app: one window over every skill this workspace's agents have. It is
the user's view of the skills catalog (`system/libs/skills_catalog`), which
reads `.agents/skills/` fresh on every request -- the one folder claude (through
the `.claude/skills` symlink), codex, pi, opencode and antigravity all read --
so a skill an agent made, changed or installed a moment ago is listed on the
next load.

One page, at `/` and at `/<name>`: a sidebar listing every skill (its name and
the first line of its description) under two headings, **Yours** (made or
installed in this workspace) and **Built in** (arrived with the template, changed
here or not), with a search field; and, beside it, the skill being read: where
it came from, its description, and its instructions rendered from its
`SKILL.md`, with the file as written behind "Show source". Choosing a skill
changes the address in place, so the shell reopens the window on it, and the
arrow keys walk the list. Below 640px the page shows the list or one skill, with
a way back. "Show in Files" opens the skills folder, or the skill's own folder,
in Files (a `file:` link the shell routes; the buttons stay hidden on a page no
shell frames, where such a link goes nowhere).

The app is read-only and stores nothing. A skill installed from elsewhere is
untrusted content, so everything the page shows from a skill is set as text,
except its instructions, which the server renders with markdown-it with raw
HTML escaped (`src/skills_app/instructions.py`). A link in them to a file
beside the skill, or to an absolute path, becomes a `file:` URL, so it opens in
Files rather than as a page of this app.

| Route | Answers |
| --- | --- |
| `GET /api/skills` | The listing (`SkillListing`). |
| `GET /api/skills/<name>` | One skill (`SkillDetail`); 404 for a name that is not a skill. |
| `GET /api/skills/<name>/instructions` | Its `SKILL.md` body rendered for reading (`RenderedInstructions`); 404 as above. |
| `GET /api/health` | Whether the app is up. |

Either listing route answers 500 with the reason when the workspace's git
history cannot be read. The API is typed through `app_schema` and published at
`/api/schema`; every model but `RenderedInstructions` and `HealthResponse` is the catalog's.

The page is one hand-written document (`src/skills_app/static/index.html`, no
build step) that reads its path and connects itself to the shell. It serves on port
8060 by the `skills` program; `SKILLS_APP_PORT` and `SKILLS_APP_HOST` are how a
preview boots.
