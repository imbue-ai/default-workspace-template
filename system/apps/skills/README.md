# skills (the app)

The Skills app: one window over every skill this workspace's agents have. It is
the user's view of the skills catalog (`system/libs/skills_catalog`), which
reads `.agents/skills/` fresh on every request -- the one folder claude (through
the `.claude/skills` symlink), codex, pi, opencode and antigravity all read --
so a skill an agent made, changed or installed a moment ago is listed on the
next load.

One page, at `/` and at `/<name>`: a sidebar (drag its edge to resize it; a
double-click restores the width) listing every skill (its name, and under it
the first line of its description) under three collapsible headings by the catalog's
`source` -- **Made in this workspace**, **Installed** (from another repository,
as a skills lock file records) and **Comes with Studio** (its author is
`imbue`) -- with a search field; and, beside it, the skill being read: a chip
for who made it ("Made by <author>", "Installed from <repo>" or "Comes with
Studio") and one for when it was last updated ("Updated 3 days ago", the date after a
week, the full time on hover), its description, and its instructions rendered from its
`SKILL.md` under the same kind of heading as the description ("Instructions,
from SKILL.md"; a first heading that only repeats the skill's name or
"Instructions" is left out), below a faint rule that sets them apart from the
description. Choosing a skill changes the address in place, so the shell
reopens the window on it, and the arrow keys walk the list. Below 640px the
page shows the list or one skill, with a way back.

Square icon buttons ask the shell for the rest, so they stay hidden on a page
no shell frames: a folder opens the skills folder (in the sidebar) or the
skill's own folder in Files (a `file:` link the shell routes); a pencil beside
the Instructions heading sends
`edit:file` with the skill's `SKILL.md`, which Files answers with its edit
page; the sidebar's + drafts "Help me make or find a new skill online that "
into the chat on screen, and a skill's "Try it" drafts `/<name> ` (the syntax
the empty pane's note explains), both unsent for the user to finish
(`shell:draft-text`: the chat on screen, else the most recent, else a new one). The page
rereads the list every 10 seconds and whenever its window takes focus or is
shown again, redrawing only what changed, so a skill made or edited elsewhere
(a chat beside it, Files) appears without a reload.

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
