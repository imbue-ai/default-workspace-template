# skills (the app)

The Skills app: one window over every skill this workspace's agents have. It is
the user's view of the skills catalog (`system/libs/skills_catalog`), which
reads `.agents/skills/` fresh on every request -- the one folder claude (through
the `.claude/skills` symlink), codex, pi, opencode and antigravity all read --
so a skill an agent made, changed or installed a moment ago is listed on the
next load.

One page, at `/` and at `/<name>`: a sidebar (drag its edge to resize it; a
double-click restores the width) listing every skill (its name, and under it
the first line of its description) under four collapsible headings -- **Made in this workspace**, **Installed**
(from another repository, as a skills lock file records) and **Comes with
Studio** (its author is `imbue`) by the catalog's `source`, then **Behind the
scenes**, folded at first, for every skill whose `audience` is `SYSTEM` (the
agents' own procedures, which have no "Try it") -- with a search field; and, beside it, the skill being read: a chip
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

**Adding a skill.** The sidebar's + opens **Add a skill** at `/?find=`, and so
does its search's last row while it holds two or more characters ("Search online
for '…'", the only row when nothing matches), with that search filled in. The
page has one field, "Search for a skill, or describe one to make": it searches
skills.sh -- the index `npx skills find` searches -- as it is typed in (after a
0.4s pause, or at once on Enter; typing replaces the address, a deliberate
search adds a history entry), and before any search offers a few to start from.
It lists ten results, most installed first, with the rest behind "Show more":
each skill's name links to its page on skills.sh (which shows what it does and
its instructions), with its source repository and an installs badge beneath,
and "Install" drafts the install request with its `npx skills add
owner/repo@name` command into the chat, so the agent installs it and reads it
first. A skill the workspace already has from that source shows "Installed" (a
link to it here); one whose name a different skill of the workspace's already
uses shows "Name in use", since installing it would land on that one. The last
row, under the results or alone when skills.sh has nothing, is "Make '…' with
your agent", which drafts a request to make it into the chat. Nothing is
searched until the person opens the page, since the query leaves the workspace;
the server asks skills.sh on the page's behalf (`src/skills_app/online_search.py`,
the page's own fetches being kept to its origin) and keeps each answer for five
minutes.

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
| `GET /api/find?q=<query>` | What skills.sh has for the query, each marked against the workspace's skills (`OnlineSkillResults`); 400 for a query under two characters, 502 when skills.sh cannot be reached. |
| `GET /api/health` | Whether the app is up. |

Either listing route answers 500 with the reason when the workspace's git
history cannot be read. The API is typed through `app_schema` and published at
`/api/schema`; every model but `RenderedInstructions`, `OnlineSkillResults` and `HealthResponse` is the catalog's.

The page is one hand-written document (`src/skills_app/static/index.html`, no
build step) that reads its path and connects itself to the shell. It serves on port
8060 by the `skills` program; `SKILLS_APP_PORT` and `SKILLS_APP_HOST` are how a
preview boots.
