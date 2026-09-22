# Plan: mngr side of editing the workspace's critical apps

The paired default-workspace-template branch `gabriel/critical-app-editing` carries the full spec at `docs/system/blueprint/critical-app-editing/plan-critical-app-editing.md`. This file is the mngr side: what this branch changes, and why the template needs it first.

## Refined prompt

Fresh paired branches in mngr and default-workspace-template, replacing the system-interface-live-editing branch, with a new spec for editing the workspace's critical apps.

* mngr side: the observe read side including the follower, the `initial_branch` widening, and notify's probe, merged from the old `gabriel/denim-pigeon` branch.
* The template runs `mngr observe` as its own supervised program (`agent-observer`) and every chat instance follows the event file, so the follower must be able to start while no observer holds the lock and report the outage until one does.
* `mngr observe --stream-events` stays; the events file is simply the second supported read side.
* The mngr PR lands first; the template vendors it.

## Overview

- The old branch made the observe event file a supported read side: a lock probe (`is_observe_writer_running`), a snapshot locator (`find_last_full_state_offset`), and `ObserveEventFollower`, a tailing thread that survives an observer restart. Its consumer was a second system interface following the live one's observer. The template's app model removed that consumer and created a better one: the chat app itself, which stops spawning an observer and follows a supervised `mngr observe` instead.
- The follower was written for a consumer that boots beside a running observer and refuses to start without one. The chat boots under supervisord beside the observer program, in no guaranteed order, so it must be able to start first and pick the stream up when the writer appears. The follow loop already rides out an outage mid-run; this extends that to the start.
- The `initial_branch` widening and notify's use of the probe are unchanged from the old branch and keep their consumers (`create_worker.py` in the template, `mngr notify`).
- Nothing else in mngr changes. `mngr observe` under supervisord needs no new flag: `--quiet` exists, the lock is released when the process exits, and the events file location is the host dir the chat already resolves through `MNGR_HOST_DIR`.

## Expected behavior

- Merging `gabriel/denim-pigeon` into this branch brings the old branch's mngr work over unchanged, with its changelog entries under `libs/mngr`, `libs/mngr_notifications`, `libs/mngr_imbue_cloud`, `libs/mngr_modal`, and `libs/mngr_vps`, renamed to this branch's name.
- `ObserveEventFollower(..., require_writer=False).start()` succeeds with no observer holding the lock. `failure_detail()` reports the outage ("no 'mngr observe' process holds the lock for ...") and `is_stream_healthy()` is False until a writer appears; the first full snapshot the new writer appends seeds the fold and both clear. `require_writer=True` (the default) keeps the old refusal.
- A follower started without a writer still seeds from the newest snapshot already in the file once a writer holds the lock, never from a snapshot an unlocked file merely happens to contain: the seed happens at the first poll that finds a writer, from one scan of the file at that moment.
- Everything else the old branch's changelogs describe holds.

## Implementation plan

- `libs/mngr/imbue/mngr/api/observe.py`: `ObserveEventFollower.require_writer: bool = Field(default=True, frozen=True)`; `start` skips `_require_a_live_writer` when False and enters the loop in the outage state; the loop's existing writer probe seeds on the first poll that finds a writer. `_seed` is called from the loop rather than from `start` in that mode, from the same single scan the mid-run re-seed uses.
- `libs/mngr/imbue/mngr/api/observe_test.py`: start without a writer, outage reported, seed and fold once a writer takes the lock and appends a snapshot; a stale snapshot in an unlocked file is not seeded from until a writer holds the lock.
- `libs/mngr/changelog/gabriel-critical-app-editing.md`: the old branch's entry with the new start mode appended. The changelog gate keys entries by branch name, so the merged `gabriel-denim-pigeon.md` entries are renamed to this branch's name in every project they cover rather than kept beside a second file.
- `dev/changelog/gabriel-critical-app-editing.md`: this spec.

## Implementation phases

1. Merge `gabriel/denim-pigeon` into `gabriel/critical-app-editing` (a merge commit; the old PR is closed unmerged).
2. Add `require_writer` and its tests; the changelog entries.
3. Open the PR; once green, the template branch regenerates `system/vendor/mngr/` from it (`just sync-vendor-mngr-live` from the template worktree) and proceeds with its own phases.

## Testing strategy

- Unit: the new `observe_test.py` cases above, alongside the old branch's follower, probe, and offset tests, which the merge brings.
- The full suite through offload (`just test-offload`), since the merge touches several projects.
- The template's system tests (`test_chat_system.py` with a real `mngr observe`) exercise the follower end to end once the template side lands.

## Open questions

- Whether `mngr observe` should gain a `--follow-parent-exit` or similar for supervised use. Today supervisord's SIGTERM is enough; revisit if the observer ever outlives the services agent.
- Whether `--stream-events` should be retired later if no consumer remains in either repo. Kept for now by decision.
