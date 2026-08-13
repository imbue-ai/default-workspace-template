`mngr observe`'s event stream now has a supported read side. `mngr observe` is
single-writer per host dir (it holds an exclusive `flock` for its whole run), but
the events it produces are appended to a plain JSONL file that any number of
processes may read. Previously only the writer was public, so a second consumer
had to either start its own observer -- losing the lock, exiting seconds into
boot, and leaving its agent view frozen forever -- or re-derive the file's
invariants itself.

`imbue.mngr.api.observe` now exports three things for that read side:

- `is_observe_writer_running(events_base_dir)` -- whether some process holds the
  observe lock. This is the liveness signal for the stream: the observer holds
  the lock for its whole run, and nothing else takes it. It never creates the
  lock file, and opens it read-only, so a missing file simply means no observer
  has ever run here and an observer whose lock file another user owns is still
  seen. If the probe itself fails it raises `ObserveLockProbeError` rather than
  answering: "we could not look" is not evidence either way, and every caller goes
  on to state whatever it gets back to a user as fact. That holds in both
  directions -- only `BlockingIOError` means "someone holds it", so a filesystem
  without `flock` (an NFS or CIFS mount returning ENOLCK) no longer reads as a live
  observer just because a stale lock file is lying around.

- `find_last_full_state_offset(events_path)` -- the byte offset of the last
  complete `AGENTS_FULL_STATE` line, or None when the file holds none yet. A
  whole-agent-set view can only be rebuilt from a full snapshot; replaying a lone
  mid-stream `AGENT_STATE` first would collapse it to the single agent that line
  names. An events file that does not exist at all raises `FileNotFoundError`
  rather than answering None: "no observer has ever run here" is a different fact
  from "no snapshot yet", and a caller may reasonably wait for the second only.

- `ObserveEventFollower` -- tails the file the running observer writes, taking no
  lock, and forwards each complete line verbatim. That is the same event sequence
  `--stream-events` prints on stdout, so a consumer folds identical events whether
  it owns the observer or follows one. It begins at the newest snapshot, forwards only
  newline-terminated lines (a snapshot exceeding the atomic-append size can leave
  a half-written tail, which is left in place and picked up once the writer
  finishes it), re-seeds if the file is truncated or replaced, and refuses to
  start at all when no process holds the lock -- silently tailing a dormant file
  is the failure it exists to prevent. It is single-use, and says so: starting a
  follower that is already running would forward every line twice, and starting
  a stopped one would leave a thread that exits immediately while still
  reporting itself live, so both raise instead.

The follower reports why it stopped rather than dying quietly. Any exit that was
not a deliberate `stop` records a failure, including exceptions raised by the
consumer's own sink (which it cannot enumerate), so a consumer gating on
`is_alive()` cannot keep reporting itself healthy after its fold has died. Those
exceptions still propagate, so the traceback survives. A stream it cannot get an
answer about says so in those words, rather than reporting the observer as exited
-- that detail is what a consumer puts in front of whoever has to fix it, and the
wrong diagnosis sends them somewhere else. `stop` holds to the same rule: a thread
still running when its wait elapses is logged rather than assumed gone, since the
only way it gets there is being stuck inside the consumer's own sink, still
delivering events to a consumer that has just been told the follower stopped. How
long that wait is, like the poll interval beside it, is the consumer's to state
(`join_timeout_seconds`): how long a sink may reasonably take to return is a fact
about the sink.

`AgentDetails.initial_branch` now reports the branch an agent's work_dir is
actually on, whether mngr created that branch or checked out one that already
existed. It previously came from `get_created_branch_name()`, which is
deliberately None for a pre-existing branch so teardown never deletes a branch
mngr did not create -- and `--branch BASE` (no `:NEW`) is exactly that case. So
for an agent deliberately placed on an existing branch, nothing in `mngr ls` said
which one, and callers were left re-deriving it from the `--branch` spec they had
passed, duplicating mngr's own parsing. Every existing consumer of the field --
`mngr_kanpan`'s PR lookup, `mngr_mapreduce`, `mngr_robinhood` -- wants the branch
the work is on, so all of them now get an answer in that case instead of none.

The same widening reaches `mngr start`'s recovery hint for a work_dir that has
gone missing: it now names the branch the work_dir was on, so an agent attached to
a pre-existing branch gets the `git worktree add` command to restore it rather than
being told no branch is recorded.

Teardown's narrower question is unaffected and still separate: `data.json` keeps
recording `created_branch_name` for "a branch we made, and may therefore delete
on cleanup", and `mngr destroy --remove-created-branch` still reads only that.
What is new is a second recorded value beside it: `CreateWorkDirResult` carries
the checked-out branch, `create_agent_state` persists it,
`AgentInterface.get_checked_out_branch_name()` reads it back, and
`DiscoveredAgent.checked_out_branch_name` covers the offline-host path. Agents
created before the field existed fall back to their created branch, which is the
only branch those records ever knew, so `initial_branch` never gets *worse* than
it was.

Providers that build `AgentDetails` by hand from `data.json` rather than through
the shared builders (imbue_cloud, vps, modal) read it the same way, via a shared
`read_checked_out_branch` helper, so the pre-existing-record fallback lives in one
place. Without that, those providers' online listings would have kept reporting
the old narrower value while their offline path reported the new one, so the
answer would have changed with host state.

Note the field is recorded at create time from what mngr placed there, and is not
re-read: an agent that checks out a different branch itself is not reflected, and
it is None for transfer modes involving no git. It is also None when the source
repo was itself on no branch (detached HEAD), which names none to inherit -- the
answer there is "unknown", so a caller that acts on the field says so rather than
being sent at a branch that does not exist. The generated CLI docs for `mngr list`
are regenerated to describe it.
