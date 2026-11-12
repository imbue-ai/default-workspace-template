# Contention between passes: superseding a pass whose worker is gone

A rare path of the harden-contention reference. Read it when a pass has no
worker left to catch up: the single-flight check found an abandoned pass, a
pass was stopped after a failure the user did not override, or an orphaned
pass's task file names another checkout as `lead_work_dir`. It names the
lead-proxy reference by role: the shared file, in this directory, a lead
follows to poll and answer its worker. `harden-creation.md` is the harden
worker's contract, `.agents/skills/harden-worker/references/harden-creation.md`.

When there is no worker left to catch up -- an abandoned pass, or one stopped
after a failure (a `stuck` on the catch-up itself included) whose user did not
override it -- replace it with **one** new pass. A worker the user's override
resumes is kept (the lead-proxy reference, "Resuming after the user overrides a
failure"). An orphaned pass whose task file names another checkout as
`lead_work_dir` is replaced the same way although its worker is still there:
its reports can never reach you, so no one here can lead it.

A pass that is gone can still have left work no one merged: commits on its
branch or on a branch of one of its own workers, a fix it finished before its
holder stopped answering. That work is never dropped. The new pass carries it:

1. **Commit what its worktrees hold.** A destroy loses uncommitted changes
   with the worktree. For the worker and each worker it launched, read
   `work_dir` from its `mngr list --format jsonl` record and run `git -C
   <work_dir> status --porcelain`; commit anything there on that worktree's
   branch, per steps 1-3 of "Last resort: manual worktree salvage" in
   `.agents/skills/launch-task/references/dead-worker-recovery.md`.
2. **Destroy it, keeping its commits.**

   ```bash
   uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py destroy --name <worker-name> --delete-branches
   ```

   Before destroying anything, the launcher checks every branch it would
   delete, the pass's and those of the workers it launched. When one holds
   commits that neither your `HEAD` nor any other branch has, it refuses and
   prints, for each, the `git branch superseded/<name> <branch>` command that
   keeps it under a name the new launch will not need. Run those commands, then
   the destroy again. The branches themselves still have to go, because the
   launch cannot reuse the worker's name while they exist. Never pass
   `--discard-unmerged` unless the user has said that work is not wanted.

   ```bash
   tk close <old-ticket-id> "Superseded -- worker gone; re-dispatched covering the union."
   ```
3. **Dispatch the new pass with that work in scope**, through your skill's
   own dispatch steps (the ticket, the task file, the launch). Its scope covers
   **everything since the last hardened merge**: at minimum the `$BASE..HEAD`
   commits touching its creations, whatever any notes on the old ticket
   describe (and any creation the notes name that the old pass did not cover,
   which the new pass covers too), and each `superseded/` branch you kept. Name
   those in the task body under a `## Carried work` heading, one per line with
   its commits (`git log --oneline HEAD..<branch>`). The worker merges them
   into its own branch before hardening and treats them as part of the change
   (`harden-creation.md`, "Carried work from a superseded pass"). They are
   merged there and not into your branch, because nothing has verified them
   yet. One superseding pass validates the union of all pending changes
   together -- which is the only combination that will actually run. A worker
   that follows its own guide rather than `harden-creation.md` (update-self's,
   migrate-workspace's) has no such step, so its new pass carries nothing:
   leave each kept branch for the user to decide on, and say so in step 4.
4. **Tell the user** what was kept, in a sentence about the work itself (from
   the commit subjects), and that the new pass is finishing it.
5. **Once you merge the new pass's `done`**, delete each branch you named
   under `## Carried work` with `git branch -d superseded/<name>`. The
   lowercase `-d` refuses a branch your `HEAD` does not contain, which would
   mean the worker never merged it: ask it why rather than forcing the delete.
   A kept branch you left for the user stays until they decide.

## A pass that had delivered a provisional milestone merge

Two wrinkles when the pass had already delivered a provisional milestone
merge:

- **A provisionally merged milestone survives.** It is already a commit on your
  own branch, so deleting the worker branch does not take it back out. The
  hardening done after that milestone is unmerged work like any other, and the
  new pass carries it.
- **A reverted milestone is carried by its revert.** If you rolled a
  provisional merge back with `git revert -m 1 <merge-commit>`, the reverted
  commits are still ancestors of your `HEAD`, so carrying the branch cannot
  bring them back, and `git log HEAD..<branch>` does not list them. The old
  worker may already have brought them back, as a revert of your revert: on
  its branch, which carrying the branch then carries, or on yours, through a
  later report of its you merged. When it had not, name the revert commit
  under `## Carried work` as well, and the new worker reverts it on its own
  branch (`harden-creation.md`, "Carried work from a superseded pass"). Never
  reinstate it on your branch yourself. Check your `HEAD` together with each
  kept branch (with none kept, `HEAD` alone):

  ```bash
  git log --oneline HEAD superseded/<name> --grep "This reverts commit <revert-commit>"
  ```

  Empty output means the old worker had not brought it back.
