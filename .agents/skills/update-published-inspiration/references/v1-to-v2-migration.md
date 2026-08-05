# Migrating a published inspiration from v1 to v2

An update always writes the current manifest format. There is no flag to stay on
v1: carrying two live write-formats indefinitely costs more than the one-time
migration, and every reader that matters is either new enough to read v2 or
reads only the markdown, which survives.

## What v1 looks like

- One or more slug-named `inspiration-<slug>.md` files at the repo root, each
  with a sibling `inspiration-<slug>.svg`.
- The recipe is a fenced `yaml` block inside the markdown, under `## Recipe`.
- No `inspiration.toml` anywhere. Absence of that file is what identifies v1 --
  not a version field inside it.
- The adaptation agenda is called `Holes`.

## What the migration does

1. `git mv` the target slug's manifest and thumbnail to `inspiration.md` and
   `inspiration.svg`.
2. Write `inspiration.toml`, lifting the recipe out of the markdown into the
   `[recipe]` table, and mirroring the front matter into `[inspiration]`.
   Add one structured entry per `requires_` line already in the markdown --
   `[[prerequisites.permission]]`, `[[prerequisites.secret]]`,
   `[prerequisites.llm]` -- so the two files agree; the validator enforces that.
3. Replace the markdown's `## Recipe` YAML block with a pointer to the TOML.
4. Rename the `## Holes` heading to `## Requirements`, and update any prose
   that refers to it. The content does not change -- only the noun.
5. Set `format: v2` in the markdown front matter.
6. Leave `[environment]` empty unless the update itself adds a dependency. A v1
   inspiration never declared one, and inventing declarations during a migration
   would be guessing at what the code needs.

## What is deliberately NOT carried forward

Any OTHER accumulated `inspiration-*.md` in the repo. v2 holds exactly one
manifest, and superseding the rest is the point of the change. Each becomes a
`[[lineage]]` entry instead -- slug, repo URL, and the commit it was used at --
which is what makes dropping the file non-destructive: the manifest stays
readable in the repo where it is authoritative.

If a superseded manifest has no recoverable repo URL and commit (it was only
ever local), say so at the scope gate rather than fabricating an entry. A
missing link is honest; a wrong one is not.
