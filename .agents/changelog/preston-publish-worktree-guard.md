`build_template.sh` now refuses to run outside a throwaway worktree.

Assembling a template resets the tree to the base version and runs `git clean
-fdxq`, which deletes untracked AND gitignored files. Pointed at a live
workspace that is `data/`, `.mngr/`, the secrets, and every scrap of runtime
state -- and the script then exits 0 and prints the next steps, so the wipe
reads as a successful publish.

What stood between that and a real mind was a sentence in the skill telling the
agent which directory to be in. That instruction cannot help on the run where
it is not followed. The script now checks two things before touching anything:
that it is not in the live workspace, and that it is in a linked worktree
rather than a main clone. Either one fails and it stops with an error naming
the fix.

`update-published-template` runs the same reset by hand rather than through the
script, so its step 4 now has the agent print `git rev-parse
--absolute-git-dir` and confirm the path is under `.git/worktrees/` before
running the reset.
