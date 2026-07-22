---
name: update-version
description: Record a version event in the workspace's `VERSION_HISTORY.md` ledger -- a `## Workspace` line when `update-self` lands a template update, an `## Inspirations` entry when `publish-inspiration` publishes. Use whenever a version event has to be written down; this skill owns the file format, which commit gets recorded, and the rules that keep a retried step from double-recording.
---

# Recording a version in `VERSION_HISTORY.md`

`VERSION_HISTORY.md` at the repo root is ONE human-readable record of where this
workspace came from and what it has published -- plain dated lines, each ending
in the commit sha that version was cut from. Two skills write to it and they must
produce identical lines, so the whole contract lives here rather than in each
skill's prose:

- `update-self` appends a `## Workspace` line when it lands a template update
  (its Step 5b).
- `publish-inspiration` appends an `## Inspirations` entry after a successful
  push -- `v1`, then `v2`, `v3`, ... for later updates of the same inspiration
  (its §8 step 4).

There is no helper program. Every operation below is a few lines of shell you run
in the repo whose ledger you are writing (`/code` for both callers), and the
ledger is the only file any of them touches.

## The rules

- **Append-only.** Every operation adds a line (or a new `### <slug>` heading).
  Existing lines are copied through verbatim -- never re-flowed, never
  re-aligned, never corrected. A ledger is a record of what was believed at the
  time it was written.
- **Every line ends in a commit.** A version entry with no sha is not an entry.
- **Idempotent.** Check whether the entry is already recorded before appending,
  so a retried step is a no-op instead of a duplicate.
- **One file, one commit.** The commit that records a version stages
  `VERSION_HISTORY.md` by name and nothing else. Never `git add -A`.

## The format

```markdown
# Version history

<the shipped explanatory paragraph -- see "If the ledger is missing" below>

## Workspace
- 2026-03-04  created from minds-v0.3.4   9f2c1ab
- 2026-05-01  updated to minds-v0.4.0     3ad77e0

## Inspirations

### people-crm  --  github.com/alice/people-crm
- v1  2026-05-02  first published                      c41b8d2
- v2  2026-06-18  added the reminders view             0e93aa7

### slack-inbox-checker  --  github.com/alice/slack-inbox-checker
- v1  2026-06-02  first published                      77b0f14
```

- A `## Workspace` line is `- <date>  <note>  <sha>`, where the note is
  `created from <version>` (exactly one per workspace, always first -- it is the
  oldest event) or `updated to <version>` (one per landed update).
- An `## Inspirations` entry lives under a `### <slug>  --  <repo-url>` heading
  and is `- v<n>  <date>  <note>  <sha>`. The version number is **computed** from
  the entries already under that slug, never typed.
- Dates are `YYYY-MM-DD`. Shas are 7 characters (`git rev-parse --short=7`).
- Notes are padded to a fixed width (26 for workspace, 35 for inspirations) so
  shas line up in the common case. A longer note just pushes its own sha right;
  earlier lines are never re-flowed to match.

## If the ledger is missing

Any operation below can run against a workspace whose ledger was deleted. Write
the starter file first, then append:

```bash
[ -f VERSION_HISTORY.md ] || cat > VERSION_HISTORY.md <<'VERSION_HISTORY_EOF'
# Version history

Where this workspace came from and what it has published. Entries are appended
automatically -- by `update-self` when it lands a template update, and by
`publish-inspiration` when it publishes -- and earlier lines are never
rewritten. Each line ends in the commit it was cut from.

## Workspace

## Inspirations
VERSION_HISTORY_EOF
```

That text is also emitted verbatim by `build_inspiration.sh` step 8.6, which
resets the snapshot's ledger so the publisher's own history never ships. Both
copies must stay byte-identical to the shipped root `VERSION_HISTORY.md`.

## 1. Seed the creation line

The `created from` line is seeded lazily -- the first time any skill writes to
the ledger -- so do this before either append below. It is a no-op once the line
exists:

```bash
grep -q "created from" VERSION_HISTORY.md && echo "creation line: already recorded"
```

If it is absent, resolve the **creation snapshot**: walk first-parent commits
from `HEAD` for the template-state markers `^update-self:` and
`Initial workspace commit`, and take the **OLDEST** one:

```bash
CREATION=$(git log --first-parent --format='%H %s' HEAD \
    | awk '{h=$1; sub(/^[^ ]+ /,""); if ($0 ~ /^update-self:/ || $0 == "Initial workspace commit") last=h} END {if (last) print last}')
# Fallback (a hand-made or pre-bootstrap repo with no marker at all): the
# FIRST-PARENT root -- never `git rev-list --max-parents=0 HEAD`, because
# subtree merges add parallel roots that are not the seed.
[ -n "$CREATION" ] || CREATION=$(git rev-list --first-parent HEAD | tail -1)
```

**This is deliberately the opposite end of the walk from `publish-inspiration`
§2's `BASE_REF`, and that difference is load-bearing.** Same markers, same
first-parent chain, opposite pick:

- `BASE_REF` takes the **NEWEST** marker -- the template state the mind is on
  *now*, which is the clean base an inspiration snapshot gets assembled on.
- The creation line takes the **OLDEST** marker -- where the mind *started*
  (normally bootstrap's `Initial workspace commit`). Every marker after it is an
  update, and each update gets its own appended `## Workspace` line. Seeding from
  the newest marker would silently claim the workspace was created at its most
  recent update and erase its actual origin.

Resolve the date and version **from the creation commit itself**, not from your
clock, so seeding late still records when the workspace was actually created:

```bash
DATE=$(git log -1 --format=%ad --date=short "$CREATION")
SHA=$(git rev-parse --short=7 "$CREATION")
VERSION=$(git describe --tags --abbrev=0 --match 'minds-v*' "$CREATION" 2>/dev/null)
NOTE="created from ${VERSION:-the workspace template}"
```

**Use `git describe` (reachability), NEVER `git tag --points-at`.** No tag is
ever *on* a creation snapshot: `Initial workspace commit` is an `--allow-empty`
commit bootstrap writes ON TOP of the cloned template commit, and an
`update-self:` marker is a merge commit. In both cases the `minds-v*` tag is on
an ancestor, so a pointing-at lookup always comes up empty and every creation
line would silently degrade to the unnamed `created from the workspace template`
fallback.

The creation line goes **first** in the section (it is the oldest event) -- the
one insert in this skill that is not an append-at-the-end, and still never
rewrites an existing line:

```bash
ENTRY=$(printf -- '- %s  %-26s  %s' "$DATE" "$NOTE" "$SHA")
ENTRY="$ENTRY" awk '{ print } $0 == "## Workspace" { print ENVIRON["ENTRY"] }' \
    VERSION_HISTORY.md > VERSION_HISTORY.md.new \
    && mv VERSION_HISTORY.md.new VERSION_HISTORY.md
```

## 2. Append a `## Workspace` entry

Used by `update-self` when it lands a template update. `$REF` is the template
version updated to; `$MERGE_SHA` is the **merge commit** -- read the trap below
before you fill it in.

```bash
DATE=$(date +%F)
NOTE="updated to $REF"
SHA=$(git rev-parse --short=7 "$MERGE_SHA")

# Idempotence: the same note and the same sha on one line means it is recorded.
if grep -F -- "$NOTE" VERSION_HISTORY.md | grep -qF -- "$SHA"; then
    echo "workspace entry: already recorded, nothing to do"
else
    ENTRY=$(printf -- '- %s  %-26s  %s' "$DATE" "$NOTE" "$SHA")
    ENTRY="$ENTRY" HEADING='## Workspace' STOP='^## ' awk '
        { l[NR] = $0 }
        END {
            for (i = 1; i <= NR; i++) if (l[i] == ENVIRON["HEADING"]) { at = i; break }
            if (!at) { print "no " ENVIRON["HEADING"] " heading" > "/dev/stderr"; exit 1 }
            for (i = at + 1; i <= NR && l[i] !~ ENVIRON["STOP"]; i++) if (l[i] ~ /[^ \t]/) last = i
            if (last) at = last
            for (i = 1; i <= NR; i++) { print l[i]; if (i == at) print ENVIRON["ENTRY"] }
        }' VERSION_HISTORY.md > VERSION_HISTORY.md.new \
        && mv VERSION_HISTORY.md.new VERSION_HISTORY.md
fi
```

The awk inserts after the section's last *content* line, so an empty section
appends directly under its heading and the blank line before `## Inspirations` is
preserved.

### The merge-sha trap

**Record the MERGE commit, never `HEAD`.** Committing the ledger is a follow-up
commit that moves `HEAD` onto the version-history commit itself. A naive re-run
that passes `HEAD` therefore passes a *different* sha, defeats the de-dupe above,
and appends a second line pointing at the ledger commit instead of at the update.

Capture the merge sha immediately after the fast-forward and before the ledger
commit. On a re-run -- when `HEAD` has already moved -- re-derive it instead of
reaching for `HEAD`:

```bash
MERGE_SHA=$(git log --first-parent --grep '^update-self:' -1 --format=%H)
```

That prints the newest `update-self:` marker, which is the merge you just landed,
and keeps printing it afterwards -- so the whole block stays safe to re-run. It
only works because the merge commit's subject keeps its `update-self:` prefix:
never give the ledger commit (or any other commit) that subject.

## 3. Append an `## Inspirations` entry

Used by `publish-inspiration` after a successful push. `$SLUG`, `$REPO_URL`
(`github.com/<owner>/<repo>`), `$NOTE` (`first published` on a first publish),
and `$SOURCE_SHA` (the source workspace commit the snapshot was cut from) come
from the caller.

First, idempotence -- scoped to this slug, since two inspirations published from
the same commit on the same day legitimately share a note and a sha:

```bash
SLUG="$SLUG" awk '
    $0 ~ "^### " ENVIRON["SLUG"] "  --  " { inside = 1; next }
    /^(## |### )/ { inside = 0 }
    inside' VERSION_HISTORY.md > /tmp/vh-slug-entries.txt
if grep -F -- "$NOTE" /tmp/vh-slug-entries.txt | grep -qF -- "$(git rev-parse --short=7 "$SOURCE_SHA")"; then
    echo "inspiration entry: already recorded, nothing to do"   # stop here
fi
```

Otherwise, create the `### <slug>` heading if this is a first publish:

```bash
if ! grep -q "^### $SLUG  --  " VERSION_HISTORY.md; then
    ENTRY="$(printf -- '\n### %s  --  %s' "$SLUG" "$REPO_URL")" \
    HEADING='## Inspirations' STOP='^## ' awk '
        { l[NR] = $0 }
        END {
            for (i = 1; i <= NR; i++) if (l[i] == ENVIRON["HEADING"]) { at = i; break }
            if (!at) { print "no " ENVIRON["HEADING"] " heading" > "/dev/stderr"; exit 1 }
            for (i = at + 1; i <= NR && l[i] !~ ENVIRON["STOP"]; i++) if (l[i] ~ /[^ \t]/) last = i
            if (last) at = last
            for (i = 1; i <= NR; i++) { print l[i]; if (i == at) print ENVIRON["ENTRY"] }
        }' VERSION_HISTORY.md > VERSION_HISTORY.md.new \
        && mv VERSION_HISTORY.md.new VERSION_HISTORY.md
fi
```

Then compute the version number from the entries already under that slug and
append under its heading:

```bash
NEXT=$(SLUG="$SLUG" awk '
    $0 ~ "^### " ENVIRON["SLUG"] "  --  " { inside = 1; next }
    /^(## |### )/ { inside = 0 }
    inside && match($0, /^- v[0-9]+/) { v = substr($0, 4, RLENGTH - 3) + 0; if (v > max) max = v }
    END { print max + 1 }' VERSION_HISTORY.md)

ENTRY=$(printf -- '- v%s  %s  %-35s  %s' \
    "$NEXT" "$(date +%F)" "$NOTE" "$(git rev-parse --short=7 "$SOURCE_SHA")")
ENTRY="$ENTRY" HEADING="### $SLUG  --  $REPO_URL" STOP='^(## |### )' awk '
    { l[NR] = $0 }
    END {
        for (i = 1; i <= NR; i++) if (l[i] == ENVIRON["HEADING"]) { at = i; break }
        if (!at) { print "no " ENVIRON["HEADING"] " heading" > "/dev/stderr"; exit 1 }
        for (i = at + 1; i <= NR && l[i] !~ ENVIRON["STOP"]; i++) if (l[i] ~ /[^ \t]/) last = i
        if (last) at = last
        for (i = 1; i <= NR; i++) { print l[i]; if (i == at) print ENVIRON["ENTRY"] }
    }' VERSION_HISTORY.md > VERSION_HISTORY.md.new \
    && mv VERSION_HISTORY.md.new VERSION_HISTORY.md
```

So the first publish is `v1` and every later update of the same inspiration is
`v(n+1)` under the same heading. Record `$SOURCE_SHA` -- the source workspace
commit captured in `publish-inspiration` §2 before dispatch -- NOT `BASE_REF` and
not anything from the worker's worktree.

## 4. Commit it

Whichever entry you appended, the recording commit is exactly one file:

```bash
git add VERSION_HISTORY.md
git commit -m "version history: <what happened>"
```

If the idempotence check said "already recorded", there is nothing staged and you
skip the commit. Never `git add -A`, and never merge, checkout, or reset as part
of recording a version.
