# Teardown (SKILL.md Step 6)

Every pass ends here, whatever its outcome. "Every ending" runs for all of
them, a needs-decision ending and a `stuck` one included. Then one of "An
ending without a landed apply" and "After a landed apply" (an apply that
exited 0). "Show the user this chat" is last for all of them.

## Contents

- Every ending
- An ending without a landed apply
- After a landed apply
- Show the user this chat

## Every ending

Drop the history bridge (a no-op when Step 3a built none; a retry rebuilds
it):

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    bridge-history --drop
```

Bring back every chat and background agent this pass paused, or an earlier
pass paused and ended without bringing back (a no-op when Step 5b's resume
already did, or nothing was paused):

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status resume-chats
```

After an apply that landed, give this resume Step 5b's `--landed` and
`--wip-commit` too, so a chat it still had to tell hears the same news.

Start the apps this pass stopped for memory (a no-op when it stopped none, or
when a landed apply's restart got there first):

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status resume-apps
```

A browser is not a program and no ending brings one back, so put any this
pass stopped back yourself (`freeing-memory-for-an-update.md`, "Putting it
back").

If `workspace-layout list` shows a preview of a critical app left registered,
tear it down as `older-workspaces.md` ("A stray preview of a critical app")
says.

## An ending without a landed apply

Release the leases either way, each as its own command: every `editing
critical app <name>` lease Step 5b took, then the `updating workspace` lease
(`tk close "$UPDATE_LEASE_ID" "<why the pass ended>"`), unless that ending's
own steps already released it (a hold, a stop at the user's request).

After a rollback, the worker's branch, worktree and report are what a retry
starts from: keep them until the retry judgment of `apply-outcomes.md`
("Exit 2") is spent.

## After a landed apply

Only for an apply that exited 0, in this order.

**Record the success verdict**: `UPDATED`, or `UPDATED_WITH_REBUILD_ITEMS`
when Step 5c left something the user must actually do or decide. A purely
deferred item with nothing observable to act on (a pending-recreate item
whose difference the user would not notice) is at most a results-message
caveat under a plain `UPDATED`, not a reason for this verdict: the app
renders it as "left something for you", and there must really be something.
When you do record it, the `--detail` line names that something in the user's
terms.

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status verdict UPDATED \
    --resulting-ref "$REF" --detail "<one plain line for the app's modal>"
```

**Stop the worker**, rather than destroying it, so its transcript stays
reachable for bug reports. If its `done` report is still in place, consume it
first (`older-workspaces.md`, "Consuming a report by hand"): a leftover one
would satisfy the next pass's `await` at once, and `launch` refuses over it.

```bash
mngr stop update-self
```

**Release the leases and close the ticket last**, each as its own tool call:

1. `tk close` each `editing critical app <name>` lease Step 5b took.
2. The `updating workspace` lease: `tk close "$UPDATE_LEASE_ID" "Update pass
   finished."`.
3. The tracking ticket: `tk close <ticket-id> "Updated to <ref> -- worker
   branch merged and applied."`, adding the `archive/update-self-<timestamp>`
   name when the launch set a previous branch aside.

## Show the user this chat

Last, whatever the outcome, once the results message (or the decision, or the
`stuck` account) is ready to read. Then end your turn with that message:

```bash
CHAT_ID="${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"
uv run --no-sync workspace-layout show chat --path "/?chat=$CHAT_ID" --showing "/$CHAT_ID" --repoint /
```

`show` raises a window already on this chat, else points a chat window on
screen at it, and opens one only when there is none. If the command fails,
carry on: the app's update row still leads the user here.
