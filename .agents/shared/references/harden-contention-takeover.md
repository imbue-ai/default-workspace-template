# Contention between passes: taking over a pass whose holder is gone

A rare path of the harden-contention reference. Read it when the single-flight
check finds a pass whose holder is gone while its worker is still there. It
names two other shared references by role, both in this directory: the
harden-contention reference it belongs to, and the lead-proxy reference a lead
follows to poll and answer its worker.

A pass whose holder stopped while its worker is still there is neither stale
nor abandoned: the worker holds verified work and is waiting for a lead, often
on a gate nobody relayed. Become that lead; do not replace the pass.

1. Its task file is the one the dispatching skill writes:
   `data/.tasks/harden/<worker-name>/task.md` for an update or heal pass,
   `data/.tasks/update-self/task.md` for update-self,
   `data/.tasks/migrate-workspace/task.md` for migrate-workspace. Its
   `lead_work_dir` must be your own checkout (`$MNGR_AGENT_WORK_DIR`; the chats
   of one workspace share it), since the worker delivers every report there.
   If it names another checkout, you cannot receive its reports: supersede the
   pass instead (the harden-contention reference, "Superseding a pass whose
   worker is gone"). An update or heal pass's `creations` are the `$CREATIONS`
   you lead it with.
2. Move the ticket to yourself, and say why on it:

   ```bash
   tk assign <their-ticket-id>
   ```

   ```bash
   tk add-note <their-ticket-id> "Holder <holder> is gone; taken over by $MNGR_AGENT_NAME. Commits <range> also change $CREATIONS."
   ```

   Then label the worker as yours. The `lead_agent` label is how the
   single-flight check finds you once you are renamed, and how the user's chat
   list files the worker under the chat leading it:

   ```bash
   mngr label <worker-name> -l lead_agent=$MNGR_AGENT_ID
   ```
3. Find what the worker last said. A report waiting at the task file's
   `finish_report_path` is unread, and the poll in step 4 hands it to you.
   Otherwise its last report is the newest file in `consumed/` beside it that
   arrived after this worker was created (an earlier pass under the same name
   leaves its reports there too):

   ```bash
   mngr list --format jsonl > /tmp/harden-agents.jsonl || exit 2
   CREATED=$(jq -r 'select(.name == "<worker-name>") | .create_time' /tmp/harden-agents.jsonl)
   [ -n "$CREATED" ] || exit 1
   find "$(dirname <finish_report_path>)/consumed" -maxdepth 1 -type f -newermt "$CREATED" \
       -printf '%T@ %p\n' | sort -n | sed -n '$s/^[^ ]* //p'
   ```

   It prints that report's path. Exit 1 means no agent has the worker's
   name, so the worker is gone: supersede the pass instead (the
   harden-contention reference, "Superseding a pass whose worker is gone").
   Exit 2 means the agent list could not be read, which says nothing about the
   worker: run the block again. No
   output means the worker has reported nothing yet. A worker that is
   RUNNING was answered (a `done` it is catching up on, say), and its next
   report reaches you through the poll in step 4. Otherwise the holder read
   that report but stopped before acting on it, so act on it now, by what it
   is:
   - A `gate` or `question` was never answered. Handle it as the lead-proxy
     reference says ("Deciding: answer gate yourself vs. escalate"). A
     `final-creation` gate goes to the user, with a sentence saying it comes
     from an earlier check of this creation that has been waiting on them.
   - A `done` was never merged: run the dispatching skill's merge for it now,
     its checks before merge first ("Before merge: lease, freshness,
     conflicts" in the harden-contention reference). They catch the pass up
     when your commits moved its base.
   - A `no-update-needed` or `stuck` is handled as the lead-proxy reference's
     "Terminal status: act and stop polling" says.

   A worker that crashed restarts with your reply, or with `continue` when
   there is nothing to answer.
4. Unless step 3 ended the pass, arm the poll (the lead-proxy reference,
   "Polling for the next report") and drive the pass to its end as its lead.
   Your own commits need nothing extra: the freshness check at merge time
   finds them, and the pass is caught up, adding any of your creations it
   does not cover (the note in step 2 names them).
