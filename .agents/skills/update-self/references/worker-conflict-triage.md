# Worker: conflicts, lockfiles and rollback reverts

Read this from the worker guide's Step 1 when a rollback revert conflicts,
and from its Step 2 when the trial merge lists a conflict or `classify-merge`
shows a lockfile changed on both sides.

## Triage each conflict

The first rule that applies wins:

- **Generated lockfiles (`uv.lock`, `package-lock.json`) -> regenerate, never
  side-pick or hand-merge.** See "Lockfiles" below.
- **Agent-owned files -> keep local** (`PURPOSE.md`, `data/`):
  `git checkout --ours -- <path> && git add <path>`.
- **Mixed files (`CLAUDE.md` and similar) -> merge by judgment.** Keep the
  agent-specific customizations, fold in the upstream additions.
  Side-picking one side of a code file outside the agent-owned rule is a last
  resort, and "ours is a superset" is a claim you must **verify, not
  assert**: diff the discarded side against the base and account for every
  change in it, as present in the kept version, or knowingly dropped and
  named as dropped in your report. A wholesale side-pick still goes through
  the 4c review gate.
- **Files untouched locally -> take upstream**: `git checkout --theirs --
  <path> && git add <path>`.
- **No clear resolution -> gate, as a last resort.** Only after you have
  genuinely tried to reconcile (both rewrote the same region incompatibly and
  the answer depends on intent you cannot see), write a `name: question` gate
  (the worker guide's Step 6) describing the file, what each side did, and
  the options. Stop. The lead decides, defaulting to this workspace's current
  behavior, and replies; apply the reply and continue.

## Lockfiles

Resolve the corresponding manifest first, then regenerate the lock from it
(`uv lock` in the lock's directory; `npm install --package-lock-only` for
npm) and `git add` the result.

**Lockfiles need attention even without a conflict.** Git auto-merges two
divergent `uv.lock`s into a semantically invalid file (duplicate
`[[package]]` entries), which has bricked a live workspace before. When
`classify-merge` shows a lockfile changed on **both** sides relative to the
base, discard the auto-merge and regenerate before committing, even with no
conflict reported.

## A rollback revert that conflicts

The apply rolls back as a *forward revert*, so after a rolled-back update
`HEAD` carries a `Roll back update apply (restore to ...)` commit on top of
the landed merge. Git then counts that release's content as already merged,
and a plain `git merge "$TARGET_REF"`, of the same release or a newer one,
lands only what the target changed since: a tree that is the old release plus
a few files, which the apply's probes cannot tell from a good update. That is
why the worker guide's Step 1 reverts each pending rollback before merging.

`pending-rollbacks` skips a rollback an earlier retry already reverted, and a
user's rollback of an app change, which is theirs to keep.

The Step 1 loop stops at the first revert that conflicts. Resolve it (a `both
added` conflict on a file the target changed since is resolved by taking the
target's version), `git revert --continue`, and run the Step 1 block again:
`pending-rollbacks` then lists only the rollbacks still left.
