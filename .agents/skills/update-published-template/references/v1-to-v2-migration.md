# Migrating a published template from v1 to v2

## Two version numbers, unrelated

The **flow version** is the manifest FORMAT. v2 is exactly one slug-free
`template.md` + `template.toml` + `template.svg` per repo; v1 is the
older format, which used slug-named `inspiration-<slug>.md` (possibly several
in one repo) with a YAML recipe inside the markdown and no TOML.

An **template's own version** (v1, v2, v3, ...) counts how many times THAT
template has been published. It keeps counting across a format migration --
a template on its fourth publish is v4 whether the format is v1 or v2.

Both appear in this skill, so read each `v<n>` for which axis it is on.

An update always writes the current manifest format. There is no flag to stay on
v1: carrying two live write-formats indefinitely costs more than the one-time
migration, and every reader that matters is either new enough to read v2 or
reads only the markdown, which survives.

## What v1 looks like

- One or more slug-named `inspiration-<slug>.md` files at the repo root, each
  with a sibling `inspiration-<slug>.svg`.
- The recipe is a fenced `yaml` block inside the markdown, under `## Recipe`.
- No `template.toml` anywhere. Absence of that file is what identifies v1 --
  not a version field inside it.
- The adaptation agenda is called `Holes`.

## What the migration does

1. `git mv` the target slug's manifest and thumbnail to `template.md` and
   `template.svg`.
2. Write `template.toml`, lifting the recipe out of the markdown into the
   `[recipe]` table, and mirroring the front matter into `[template]`.
   Add one structured entry per `requires_` line already in the markdown --
   `[[requirements.permission]]`, `[[requirements.secret]]`,
   `[requirements.llm]` -- so the two files agree; the validator enforces that.
   A v1 `Holes` bullet becomes a `[[requirements.adaptation]]` entry.
3. Replace the markdown's `## Recipe` YAML block with a pointer to the TOML.
4. Merge `## Holes` and `## Prerequisites` into a single `## Requirements`
   section. The content does not change and nothing is dropped: the
   `requires_` lines keep their exact form (they are the activation half, and
   the adopting agent still acts on them first and by itself), and the Holes
   bullets follow as the adaptation half. v1 kept these apart as two headings
   with near-synonym names, which made filing each item correctly a human
   judgement; v2 makes the kind a property of the entry instead.
5. Set `format: v2` in the markdown front matter.
6. Leave `[environment]` empty unless the update itself adds a dependency. A v1
   template never declared one, and inventing declarations during a migration
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
