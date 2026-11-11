# Contention between passes: catching up a stale pass

A conditional path of the harden-contention reference. Read it when a check
before a merge found the pass stale (a moved base, a conflicted merge, an apply
refused because `HEAD` moved), or when a note on the pass's ticket names a
creation the pass does not cover. It names the lead-proxy reference by role:
the shared file, in this directory, a lead follows to poll and answer its
worker.

The pass goes back to its worker, which you keep: the harden-contention
reference's "Catching up a stale pass" says why. Ask the worker with a reply
naming your current `HEAD` and what moved, then re-arm the poll exactly as for
a gate (the lead-proxy reference):

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py reply \
    --task-file <TASK_FILE> -m "Catch up to $(git rev-parse HEAD) per 'Catching up to a moved base' in harden-creation.md: merge it into your branch, resolve any conflict, harden and verify what it brings in, and report done again. What moved: <the \$BASE..HEAD commits touching its creations, a conflicted path, and any notes on the ticket>. Add to the pass: <each creation a note names that the pass does not cover, as type:name>."
```

Leave out the `Add to the pass:` sentence when no note names one. When one
does, add those creations to your own `$CREATIONS` too, so the freshness check
and go-live cover them from then on, and record them where other chats read
the pass: your task file's `creations` line, which a chat taking the pass over
leads it with, and the ticket's title, which the single-flight check
matches. A ticket's title is the first `# ` line of its file:

```bash
ADDED="<each added creation, as type:name>"
sed -i "s|^creations: .*|& $ADDED|" <TASK_FILE>
sed -i "0,/^# /s|^# .*|& $ADDED|" "$TICKETS_DIR/<ticket-id>.md"
```

The worker's side is `.agents/skills/harden-worker/references/harden-creation.md`
("Catching up to a moved base"), or its own flow's catch-up section where one
exists (`update-self`'s worker has its own). It merges your `HEAD` into its
branch, so merging that branch back into yours conflicts with nothing. It
resolves every conflict on its side, regenerating lockfiles rather than
choosing sides. The commits it brings in that touch a creation in the pass get
the same hardening as the rest of the pass, and its test gate re-runs over
what the catch-up changed for the pass, not over every other chat's commits in
the window. Then it reports `done` again.

That report goes through the same checks before merge as the first one.
Usually it is fresh now. If `HEAD` moved again while the worker caught up,
catch it up again: each round covers only the commits since the last, so the
rounds shrink. A worker that cannot reconcile the new base reports `stuck` or
asks a `question`, handled as on any run.

A provisional milestone you reverted needs nothing more here: its worker
already brought the milestone's work back on its own branch when you handed it
the revert ("Rolling back a provisional merge", in the lead-proxy reference's
milestone file).
