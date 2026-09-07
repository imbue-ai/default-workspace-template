# Scaling the transcript UI to long conversations

How the `system_interface` chat stays responsive and memory-light as conversations grow.
(This doc originally specified a two-tier evicting cache on the backend; that design was
replaced by the full-residency store described here -- see
`docs/system/blueprint/simplify-chat-data-model/` for the replacement's rationale and
plan. The frontend sections still describe the live design.)

## Backend: a payload-free resident store

The watchers (`harnesses/transcript_store.py` and the per-harness subclasses) keep one
agent's whole parsed transcript resident: ordered lanes of event dicts (one lane per
session file for claude, a single merged timeline for codex/pi) plus one agent-wide
`event_id` index. Residency is cheap because the events are **payload-free**: tool
inputs, tool outputs, and thinking never leave the disk. What the default render needs is
stamped at parse time (labels derived from the full input, `input_chars`/`output_chars`,
`error_snippet`, `tk_command`/`tk_stamp`, `permission_request`, `has_thinking`), and the
whole payloads are served on demand by `GET /api/agents/<id>/events/<event_id>/detail`,
which re-reads the event's recorded source byte range statelessly -- nothing is cached
backend-side.

The paged read API is unchanged: `GET /api/agents/<id>/events` serves the tail by
default, `?before=`/`?after=` page in either direction, `?offset=` jumps, and every
response carries `offset` + `total` so the client places its window in the whole
conversation. All of them are O(limit + session-file count) over the resident store.

Memory lifetime is owned by watcher eviction: when an agent is destroyed or its lifecycle
transitions into a positively-dead state (a UI stop, `mngr stop`, an OOM shed, idle
shutdown), the agent manager's composition-wired callback pops and stops its watcher --
the resident transcript, watch thread, and inotify watches go with it. Viewing a stopped
chat rebuilds the watcher from disk on demand.

Live delivery is a hint layer, never the source of truth: per-connection SSE queues are
bounded, an overflowing consumer is disconnected on the first full `put`, and the
frontend's reconnect-with-snapshot (reopen the stream, buffer deltas, refetch the tail)
resyncs it. Activity signals fold incrementally from each delivered batch -- the tracker
never re-reads the transcript per event.

## Frontend: bounded memory, on-demand backfill, virtualization

### Event store (`Response.ts`)

`eventsByAgent` holds the resident events per agent, mirrored by a persistent
`eventByIdByAgent: Map<event_id, event>`. The map serves two purposes at O(1)
per event: dedup on append/prepend (no rebuilding a set on every SSE delivery),
and lookup of an already-stored event so a re-broadcast (same `event_id`, e.g. a
subagent tool-call whose linkage arrived late) can be upgraded in place rather
than dropped as a duplicate.

`fetchEvents` loads the tail; backfill/forward paging and offset jumps ride the
`offset`/`total` the server reports. `evictOldEvents` trims the oldest events once the
resident count exceeds `MAX_HELD_EVENTS` (1500) down to `EVICT_TARGET_EVENTS` (1000), so
the dropped history is re-fetched on scroll-up. The callers only evict while following
the live tail, so a scrolled-up reader's rendered history is never removed from under
them.

Deferred payloads live in a separate per-agent detail cache (`requestEventDetail` /
`getEventDetailState`): fetched the first time a tool row or thinking disclosure is
expanded, kept for the page session, and surviving virtualization remounts alongside the
expansion state itself.

### Windowing (`virtualWindow.ts`, `ChatPanel.ts`, `SubagentView.ts`)

`computeVisibleWindow` is a pure, DOM-free function: given the row count, a
`getHeight(index)` accessor, the scroll position, the viewport height and an overscan
margin, it returns the contiguous slice of rows intersecting the viewport plus the
`topPad`/`bottomPad` spacer heights standing in for the rows above and below. The message
list mounts only those rows; heights are measured after mount and cached, with per-type
estimates for unmeasured rows. Scrolling near the top triggers exactly one backfill page;
on prepend, `scrollTop` is compensated so the viewport stays anchored.
