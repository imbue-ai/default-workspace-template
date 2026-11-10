# about_me (the app)

What Studio knows about me: one page showing what the opt-in web lookup by name
(the `personalized-ideas` skill) remembered about the user, so they can see and
delete all of it without asking. The design is the "A 'What Studio knows about
me' page" section of `docs/system/specs/personalized-ideas/spec.md`.

- **Each fact** in `data/memories/user-public-profile.md`, one row per line
  that starts with `- `: the fact, its source as a link (or "You told me" for
  a fact the user shared), the date it was found, and a Delete button. A line
  in neither of the skill's fixed shapes is still a fact, shown as written and
  deleted whole; the frontmatter and the closing plain sentence are not facts.
- **Delete** removes exactly that line (every other byte of the file stays as
  it was) and, from `data/.skills/personalized-ideas/sources.json`, every entry
  whose `url` the deleted line cited and no remaining line cites (and the
  user's own null-`url` entries once no "(you told me)" fact remains).
  Deleting the last fact forgets everything.
- **Forget all of this** (after a confirm) deletes the profile, removes its
  line from `data/memories/MEMORY.md` (the one naming
  `(user-public-profile.md)`; the rest of the index is untouched) and deletes
  `sources.json`, as asking the agent to forget does.
- An empty state when nothing is remembered, saying how to ask for a lookup,
  and a "View raw file" disclosure with both files as they are on disk.

Each delete names its fact by the line's index and a hash of its bytes, so a
page left open while the agent rewrote the memory gets a 409 and reloads
instead of deleting another line. Writes go through a temporary file renamed
over the original, under one lock. The app reads and writes only those three
files, relative to the workspace root it runs from (`ABOUT_ME_WORKSPACE_DIR`,
the working directory by default), and never reaches the network: the page
fetches only this app's own routes.

The page is plain HTML (`src/about_me/static/`) on the page kit the shell
builds (`/_static/workspace_theme.css` and `.js`), with its own layout on the
design tokens only, so it wears every workspace theme (`[theming] mode =
"tokens"`).

The app describes its own HTTP API at `GET /api/schema`: `GET /api/profile`
(the facts and the raw files), `POST /api/facts/delete` (`{"fact_id"}`; 409
when the memory changed since the page read it), `POST /api/forget`, and
`GET /api/health`.

Served on port 8070 by the `about-me` program.
