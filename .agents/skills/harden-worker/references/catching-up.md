# Catching up after your lead's branch changed

A conditional part of the harden contract (`harden-creation.md`, beside this
file). Read it when your lead's reply asks you to catch up to a commit, or
names a revert of a milestone it had merged.

## Contents

- Catching up to a moved base
- A reverted milestone

## Catching up to a moved base

After your `done`, the lead may find that its `HEAD` moved before it could
merge you, and reply asking you to catch up to a named commit (the lead's side
is "Catching up a stale pass", in the shared harden-contention reference). Your
verified work stands; extend it to the new base rather than
starting over.

1. Commit anything outstanding and merge the lead's commit into your branch:

   ```bash
   git merge --no-ff <the commit the lead named> -m "Catch up to <its short sha>"
   ```

2. Resolve every conflict on your branch. Regenerate a lockfile from its
   merged manifest (`uv lock`, `npm install --package-lock-only`), never pick
   a side of it. For anything else, the commits you are bringing in are the
   user's latest word: keep what they do and fit your hardening around them.
   A conflict whose answer depends on intent you cannot see is a `question`
   gate. Conclude the merge with `git commit --no-edit`, which keeps the
   subject step 4 finds it by.
3. **Harden what came in.** Commits that touch a creation in the pass are now
   part of the change you are hardening, under the whole harden contract: tests
   that cover their behavior, its invariants, and anything your
   operation reference requires of a change (a critical app's bundles are
   rebuilt and re-reported, `type-app.md`). Commits that touch none of them
   need nothing from you. When the reply names creations to add to the pass
   (another chat's change reached past what yours covers), add each to the
   `creations` line of your task file's frontmatter (`$TASK_FILE`), so every
   later parse includes it, load its type's reference if no creation of that
   type was in the pass, and harden it as part of the change.
4. **Re-run the test gate over what the catch-up changed for the pass**,
   after committing. Not over everything since `$PRE`, your branch's tip
   before the catch-up merge: in a shared workspace most of what came in is
   other chats' work on other creations, which step 3 leaves alone. Write a
   scope file for the catch-up alone, beside `$SCOPE_FILE` rather than over
   it, and run the gate from that:

   ```bash
   MERGE=$(git log --first-parent --merges -1 --format=%H --fixed-strings --grep "Catch up to")
   PRE=$(git rev-parse "$MERGE^1")
   LEAD_HEAD=$(git -C "${LEAD_WORK_DIR:-$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")}" \
       rev-parse HEAD) || exit 1
   CATCH_UP_SCOPE="$(dirname "$SCOPE_FILE")/catch-up-scope.json"
   uv run --no-sync app-manifest pass-scope $(printf -- '--creation %s ' $CREATIONS) \
       --diff-base "$PRE" --lead-ref "$LEAD_HEAD" --out "$CATCH_UP_SCOPE" || exit 1
   uv run --no-sync app-manifest select-tests --scope-file "$CATCH_UP_SCOPE"
   ```

   The commit you merged is the fork point now, so what came in with it is
   the lead's work. It selects where a commit of it touched a creation in the
   pass (step 3's), where your branch changed the same file (every conflict
   is one), and, through the scope file's `incoming`, where it reaches a
   creation in the pass through something that creation depends on (a
   package, a `uv.lock` upgrade, a shared frontend package, or the root
   project's own `pyproject.toml` or `conftest.py` for a creation the root
   project collects): that runs the creation's own suite and nothing else,
   or for a shared script, which has none, the full root suite.
   What you committed after the merge is yours, selected like any change. A
   selection with nothing to run means no part of the pass moved. Do not
   re-run the gate over `$DIFF_BASE`: what it already passed still stands,
   unless step 3 added a creation, whose changes before `$PRE` this window
   does not hold; then run the gate from the regenerated `$SCOPE_FILE`
   instead, as in "The test gate" of the harden contract. For a test that
   fails in this run, the tree before your change ("A failing test you would
   change", in `test-gate-cases.md` beside this file) is the commit
   you merged, the catch-up merge's second parent, not `$DIFF_BASE`: a test
   that fails there was already failing at the lead's `HEAD`, and one absent
   there is your pass's own.
5. Regenerate `$SCOPE_FILE` and report `done` again, with a `Caught up:`
   section naming the commit you merged, each conflict and how you resolved
   it, any creation you added, and what the re-run gate ran. The lead's work
   that came in is in the scope file's `others`, not your
   `outside_footprint`.

## A reverted milestone

When the lead reverts a milestone it merged, its reply names the revert
commit and says why. Bring the milestone back on your branch as
`worker-reporting.md`'s "A milestone the lead reverted" says, resolve any
conflict as step 2 of "Catching up to a moved base" says, then fix the
milestone's work per the lead's reply.

The revert of the revert is yours, selected like any change, so your test
gate covers the milestone's work again. A pass still under way goes on as
before; one that had already reported `done` finishes with steps 3 to 5 of
that section, where the tree before your change is the lead's revert commit,
not the merge's second parent, which holds the milestone again. Either way
the work reaches the lead only with the next report of yours it merges.
