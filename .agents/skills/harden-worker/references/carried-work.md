# Carried work from a superseded pass

A conditional part of the harden contract (`harden-creation.md`, beside this
file). Read it when your task body has a `## Carried work` section, before you
start hardening.

A task body with a `## Carried work` section replaces an earlier pass that
ended without being merged, and names the branches its lead kept that pass's
commits on (the lead's side is "Superseding a pass whose worker is gone", in
the shared harden-contention reference). That work is part of your change.
When the section also names a
revert of a milestone the earlier pass had merged, revert that revert first: a
merge cannot bring back commits the lead's `HEAD` already holds, and a carried
branch's later edits to the milestone merge cleanly only onto the milestone
itself, so merging them before the revert would set them against its removal.
What returns is carried work like the rest:

```bash
git revert --no-edit <the revert commit>
```

Then merge each branch into yours before you start hardening:

```bash
git merge --no-ff <the carried branch> -m "Carry over <the carried branch>"
```

Resolve a conflict as a catch-up does (step 2 of "Catching up to a moved
base", in `catching-up.md` beside this file), except that here your side is the
newer one: the carried commits are older work to fit around what the user has
done since. Then harden them under the whole harden contract like the rest of
the change. Keep each one that still holds, fix one that does not, and `git
revert` one the current base has made wrong, so every carried commit ends up
merged either way. From then on they are commits on your branch, so the scope
file's diff and the test gate cover them with nothing extra. Your `done` report
gets a `Carried:` section: each carried commit, and whether you kept, changed
or reverted it, and why.
