# Resuming after a decision

When the user answers a needs-decision ending (SKILL.md Step 4) in this chat, minutes or
days later, re-land the kept pass rather than starting a fresh one. The worker
is stopped with its conversation intact and its branch `mngr/update-self`
still holds the validated merge, so the decision is one more round of the
SKILL.md Step 4 cycle, not a new pass.

1. Check that the kept pass is still this chat's. If
   `data/.state/update-apply/run.json` names another chat, or `git rev-parse
   --verify --quiet refs/heads/mngr/update-self` prints nothing, a later
   update has started since and set this one aside (its launch archives the
   branch): tell the user so, and that the newer update's own chat is where
   it stands. Do nothing else.
2. If they choose to skip the update, record `run-status verdict REFUSED
   --detail "<what the update could not keep, and that they chose to keep
   it>"` with the staged script and end; the worker stays stopped, and the
   next pass's launch sets its branch aside.
3. Otherwise re-take the pass's preconditions, each `tk` call as its own
   command: the `updating workspace` lease, exactly as SKILL.md Step 1 takes it; then
   the run record and the target the task file carries:

   ```bash
   python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
       run-status start
   REF=$(sed -n 's/^target_ref: //p' data/.tasks/update-self/task.md)
   ```

   then the history bridge, with SKILL.md Step 3a's two commands (the forced tag fetch,
   then `bridge-history --ref "$REF"`).
4. Check whether the kept branch still fast-forwards. It usually does not:
   the workspace kept working while the decision waited.

   ```bash
   git merge-base --is-ancestor HEAD mngr/update-self
   ```

5. Give the worker the decision, in the user's voice, which starts the
   stopped worker. On exit 1 above, ask in the same message for the catch-up
   SKILL.md Step 5b asks for, so it re-fits and catches up in one round; on
   exit 0, send the decision alone:

   ```bash
   MESSAGE="<their decision>. Then catch up to $(git rev-parse HEAD) per Step 7 of your worker guide, and report done."
   WORKER_ID=$(sed -n 's/^worker_agent_id: *//p' data/.tasks/update-self/task.md)
   python3 system/scripts/message_chat.py "$WORKER_ID" -m "$MESSAGE"
   ```

   `applying-the-update.md` ("Messaging the worker") reads its exit code, and
   says how to send when the workspace has no `message_chat.py`.
6. `run-status delegate update-self`, then re-arm the `await` poll
   (`launching-the-worker.md`, section 5) and end your turn. The `done` it
   brings takes SKILL.md Step 5 like any other: the audit (with its Catch-up
   section), then Step 5b, whose fast-forward check catches the worker up
   again if `HEAD` moved meanwhile, and a verdict at the end.
