# about_me (the app)

What Studio knows about me: one page that is the user's view onto the
workspace agent's own memory, Claude's built-in memory in `data/memories/`
(one memory per file, indexed by `MEMORY.md`). It keeps no store of its own,
so what it shows is exactly what the agent reads at the start of every chat.
The design is the "A 'What Studio knows about me' page" section of
`docs/system/specs/personalized-ideas/spec.md`.

- **Every memory**, grouped by its frontmatter `type`: About you (`user`),
  How you like to work (`feedback`), Your projects (`project`), Links and
  references (`reference`), Other notes (anything else). Each shows its
  `MEMORY.md` title and its text, with **Edit** (its description and body,
  and its index line's hook; every other frontmatter line stays) and
  **Delete** (the file and its index lines).
- **The web lookup's memory** (`user-public-profile.md`, written by the
  `personalized-ideas` skill), fact by fact under About you: each line with
  its source link and date, its own **Edit** (words only) and **Delete** (the
  line, and the `data/.skills/personalized-ideas/sources.json` entries no
  remaining line cites), and **Forget what the lookup found** (the file, its
  index line and `sources.json`).
- **Add something about yourself** writes a new `user` memory in the format
  the agent writes its own, with its index line.
- **Forget everything** (after a confirm) deletes every memory, `MEMORY.md`
  and `sources.json`.
- An empty state, and a "View raw files" disclosure with `MEMORY.md`, the
  lookup's memory and `sources.json` as they are on disk.

Each change names its target by a hash of the file's (or the line's) bytes,
so a page left open while the agent rewrote a memory gets a 409 and reloads
instead of overwriting it. Writes go through a temporary file renamed over
the original, under one lock. The app reads and writes only the memory
directory and `sources.json`, relative to the workspace root it runs from
(`ABOUT_ME_WORKSPACE_DIR`, the working directory by default), and never
reaches the network: the page fetches only this app's own routes.

The page is plain HTML (`src/about_me/static/`) on the page kit the shell
builds (`/_static/workspace_theme.css` and `.js`), with its own layout on the
design tokens only, so it wears every workspace theme (`[theming] mode =
"tokens"`).

The app describes its own HTTP API at `GET /api/schema`: `GET /api/profile`
(every memory, the lookup's facts, and the raw files), `POST /api/memories/add`
(`{"text"}`), `POST /api/memories/edit` (`{"file_name", "revision",
"description", "body"}`), `POST /api/memories/delete` (`{"file_name",
"revision"}`), `POST /api/facts/edit` (`{"fact_id", "text"}`),
`POST /api/facts/delete` (`{"fact_id"}`), `POST /api/forget`
(`{"scope": "lookup" | "everything"}`), and `GET /api/health`. A change is a
400 when its text cannot be kept, and a 409 when its target changed since the
page read it.

Served on port 8070 by the `about-me` program.
