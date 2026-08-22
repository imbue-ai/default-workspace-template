# Chat scroll virtualization rewrite

Replace the chat transcript's hand-rolled virtualization with `@tanstack/virtual-core`, and
size unloaded history from measured geometry instead of a fixed per-event constant.

Predecessors: [scaling-design](../scaling-design/plan-scaling-design.md) (introduced the
virtualization), [chat-scroll-selection-fixes](../chat-scroll-selection-fixes/plan-chat-scroll-selection-fixes.md)
(the last major repair).

## Overview

- The transcript jumps while reading history because **the reservation unit does not match the
  render unit**. `ChatPanel` reserves `ESTIMATED_EVENT_HEIGHT_PX = 160` per *unloaded event*, but a
  tool-heavy turn collapses into one `ProgressBlock` row rendering at roughly 34px/event.
- A 50-event backfill page therefore lands 2–4k px shorter than reserved, in one frame. That exceeds
  `OVERSCAN_PX = 800`, unmounts the row native scroll anchoring was holding, and the viewport
  teleports.
- Five prior fixes (#100, #155, #218, #241, #264, #370) each removed a real mechanism and re-broke
  something else. Every one layered a *compensator* on top of the unit mismatch. PR #448 tried four
  compensators in four commits and was closed after reintroducing the #218 hidden-tab bug.
- This plan removes the mismatch instead of compensating for it: measure each row once, persist the
  measurement, and reserve space from real cumulative heights.
- Adopt `@tanstack/virtual-core` (MIT) for measurement plumbing, snapshot/restore, prepend-safe keys
  and scroll compensation. Keep phantom sizing, follow-state and turn-grouped rows in-house — those
  are ours and the library has no concept of them.
- Scope is a **robust MVP**. Render-cost work (markdown HTML caching, redraw scoping, incremental
  turn-grouping) is explicitly deferred; smoothness here comes from correct geometry, not cheaper
  frames.

## Expected behavior

- Scrolling up through history never moves the content the user is reading. Pages land where the
  geometry already said they would.
- Dragging the scrollbar deep into a long conversation lands silently at the right place — no
  spinner, no visible window reset.
- Spurious jumps disappear. A jump now fires only when the user actually drags past
  `JUMP_GAP_EVENTS`, because the viewport→offset mapping is derived from real heights.
- At the tail, new messages scroll into view; a gentle scroll up detaches cleanly and stays detached
  through streaming.
- A text selection survives scrolling and streaming at any distance, including when its rows are far
  off-screen.
- An inactive dockview tab keeps its place, unchanged from today.
- Revisiting a conversation on the same device is accurate immediately — heights come from
  IndexedDB.
- A first visit on a *new* device is accurate immediately when the server has geometry for it;
  otherwise it settles within the first screens, and the settling never moves the viewport the user
  is reading.
- `SubagentView` behaves identically, since it shares the pipeline.
- No user-visible change to turn grouping, message rendering, permission cards, or subagent cards.

## Implementation plan

### New frontend modules

- **`frontend/src/models/rowGeometry.ts`** — pure, DOM-free geometry model.
  - `RowGeometry { row_key, start_offset, end_offset, height }` — a row plus the global event range
    it covers.
  - `RowGeometryIndex` — rows sorted by `start_offset` and never overlapping, with cumulative sums
    over both height and event count maintained alongside.
  - `heightBefore(offset, gapRate)` — binary search + prefix-sum; event ranges no row covers are
    priced at `gapRate`, and coverage is allowed to be sparse (reading the head and jumping to the
    tail leaves a genuine hole, which is a normal state rather than one to repair).
  - `offsetAtHeight(height, maxOffset, gapRate)` — the exact inverse, binary-searched over
    `heightBefore` itself at the same rate so the two cannot drift apart.
  - The rate is the **caller's**, not derived here, and the fixed `160` survives only as
    `DEFAULT_EVENT_HEIGHT_PX` for a genuinely cold first paint. This index admits a row only once it
    has *settled*, so a rate taken from it would sit at the cold value until the first settle landed
    and then collapse in one step — on whichever redraw came next, routinely the user's own scroll.
    `ChatPanel.updateReserveRate` learns it instead, from every row measured at all, as an
    event-weighted mean over the whole loaded window rather than a median over the measured ones (a
    median lurched as short user bubbles settled ahead of tall assistant rows).
  - `recordRow(row)` — replaces every row whose range the new one overlaps, so the same rendered row
    re-recorded at a different range (the loaded window moved) cannot be filed twice.
- **`frontend/src/models/geometryCache.ts`** — IndexedDB persistence ("measure once").
  - Keyed `agentId:widthBucket`, value is the `RowGeometry[]` plus a timestamp.
  - Viewport width quantized into 64px buckets, so a scrollbar appearing keeps the cache warm and a
    real layout change misses.
  - Bounded to ~50 conversations, 30-day TTL, least-recently-used eviction.
  - Degrades to in-memory when IndexedDB is unavailable (private browsing, quota).
- **`frontend/src/models/workspaceGeometry.ts`** — server sync for the cold-start seed.
  - `loadWorkspaceGeometry(agentId, widthBucket)` / `saveWorkspaceGeometry(...)`.
  - Local always wins; the server is read only when there is no local entry.
- **`frontend/src/views/transcriptVirtualizer.ts`** — the Mithril↔TanStack adapter.
  - Supplies `observeElementRect`, `observeElementOffset`, `scrollToFn` (the three things a framework
    adapter must provide) and wires the virtualizer's `onChange` to `m.redraw()`.
  - Options: `count` from rows; `getItemKey` returning `rows[i].key` (prepend-safe); `estimateSize`
    reading the measured height then the learned estimate; `paddingStart`/`paddingEnd` from
    `heightBefore()`; `rangeExtractor` unioning the visible range with the selection's row indices.
  - Items stay in normal flow between two spacers rather than absolutely positioned, because the
    transcript relies on the browser's own scroll anchoring, and overscan is expressed in pixels
    rather than in items.
  - `shouldAdjustScrollPositionOnItemSizeChange` **disabled**: the reserved space for unloaded
    history moves at the same time as the rows do and the two routinely cancel, so the view holds
    the reader's place by anchoring on the row being read instead. Two writers of `scrollTop` for
    overlapping reasons double-correct.
  - Sizes are reported by index rather than read from the DOM by the library, so the measurement
    hysteresis below stays in the loop.
- **`frontend/src/views/rowMeasurement.ts`** — measurement and its trustworthiness gate.
  - One frame-debounced pass over the mounted rows, reading `getBoundingClientRect().height` (not
    `offsetHeight`, which is device-pixel-snapped and so flips by a pixel as a row drifts).
  - A row becomes trustworthy — and only then is persisted — after a quiet window (~500ms).
  - Retains a hysteresis threshold around measured deltas (see Open questions — this is what fixed
    the #264 1px jitter and TanStack does not provide it).

### Modified frontend files

- **`frontend/src/views/ChatPanel.ts`** — thin wiring only, to minimize conflicts with the in-flight
  harness/composer work on this file.
  - Delete `phantomTopHeight`/`phantomBottomHeight` arithmetic and `ESTIMATED_EVENT_HEIGHT_PX`.
  - Delete the local `computeTranscriptSlices` call; render from the virtualizer's items.
  - `maybePage` keeps its three branches but derives the target offset from `rowGeometry`, making it
    the exact inverse of the reservation.
  - Keep eviction, the `panelVisible` gating, drag-and-drop, and the 404 retry untouched.
- **`frontend/src/views/SubagentView.ts`** — same virtualizer, zero reserved space.
- **`frontend/src/views/conversation-rows.ts`** — `buildConversationRows` gains the event range each
  row covers; `renderTranscriptSegments` replaced by `renderVirtualRows`. Rows keep their DOM `id`.
- **`frontend/package.json`** — add `@tanstack/virtual-core`.

### Deleted frontend files

- `frontend/src/models/virtualWindow.ts` and `virtualWindow.test.ts` — superseded; invariants ported.
- `frontend/src/views/row-measurement.ts` and `row-measurement.test.ts` — superseded by
  `views/rowMeasurement.ts`, which keeps measurement in-house rather than letting the library read
  the DOM.
- **Retained**: `views/transcript-scroll.ts`. Windowing and measurement move out of it, but it stays
  the single owner of the one decision every previous attempt got wrong — whether to follow the tail
  — along with the pointer-drag and resize lifecycle.
- **Retained**: `models/scrollFollow.ts` unchanged. `nextUserScrolledUp` is a locked contract with 11
  tests that four merged PRs depend on; the virtualizer feeds it rather than replacing it.
- **Retained**: `views/scroll-selection.ts`, feeding row indices to `rangeExtractor`.

### Backend

- **`imbue/system_interface/transcript_geometry.py`** (new, ~170 lines) — mirrors
  `member_last_used.py`: a JSON file under the workspace layout dir, written under a module-level
  lock, created on first touch.
  - Dumb store only. The server never derives heights or row counts — turn grouping lives in
    `turn-grouping.ts` and must not be reimplemented in Python.
  - Keyed by agent id plus width bucket; value is the client-computed row/height table. Bounded on
    every axis it can grow along — stored transcripts, width buckets per transcript, rows per entry
    — and an agent's entry is dropped when the agent is destroyed.
- **`imbue/system_interface/server.py`** — two routes via `add_url_rule`:
  - `GET /api/agents/<agent_id>/geometry`
  - `PUT /api/agents/<agent_id>/geometry`

### Changelog

- `system/apps/system_interface/changelog/preston-improved-rendering-scrolling.md`.

## Implementation phases

1. **Geometry model.** `rowGeometry.ts` with unit tests. Pure, unwired — the system is unchanged and
   still ships.
2. **Verification harness.** New acceptance-marked Playwright test exercising real scroll geometry
   against a tool-heavy transcript fixture. Built before the swap so every later phase is measurably
   non-regressing. It asserts content-relative stability outright rather than recording a baseline to
   compare against.
3. **Virtualizer swap.** Add the dependency, write the Mithril adapter, move `ChatPanel` onto it,
   delete `virtualWindow.ts` / `row-measurement.ts`. Behavior parity is the bar here — phantoms
   still sized by the old constant. Port the retired invariants.
4. **Prefix-sum reservation.** Replace `ESTIMATED_EVENT_HEIGHT_PX` with `heightBefore()`, and make
   `maybePage`'s mapping its exact inverse. **This is the jump fix**; the harness should show the
   jump disappear here.
5. **Persistence.** `geometryCache.ts` (IndexedDB, settle gate, width buckets, TTL/LRU), then
   `workspaceGeometry.ts` and the backend module/routes for the cold-start seed.
6. **SubagentView migration and cleanup.** Move the second view across, remove now-dead code,
   changelog.

Each phase leaves a working system; all six land as one branch (`preston/improved-rendering-scrolling`).

## Testing strategy

- **Unit (vitest).** `rowGeometry.ts`: prefix-sum correctness, binary search on row boundaries,
  replacement by range, `offsetAtHeight` pricing its gap at the same rate that sized it, sparse
  coverage with a hole between measured islands. `geometryCache.ts`: width bucketing, TTL, LRU
  eviction, IndexedDB-unavailable fallback.
- **Ported invariants.** From `virtualWindow.test.ts`: pad/total consistency, the disjoint pinned run
  (a far-off selection must not mount the rows between), phantoms folded into outer spacers,
  caller-supplied pin ranges clamped internally. Its backward fill when scrolled past the end is not
  ported: the range calculation is the library's now, and it reads the live `scrollTop` rather than a
  cached one, so there is no longer a window of ours that can be asked about a position the browser
  has already clamped away. From `row-measurement.test.ts`: sub-pixel wobble must never ratchet a
  cached height across the threshold, and measurement must debounce to a single frame.
- **Acceptance (Playwright, in CI).** The new harness, marked `@pytest.mark.acceptance`:
  - Content-relative viewport position is stable while paging history — tracked as *row id at
    viewport top + offset*, not `scrollTop`. The earlier investigation found 4 of 6 real jumps had a
    `scrollTop` delta of exactly zero, so `scrollTop` tracing is blind to this bug.
  - Tail-follow attaches and detaches: new messages scroll in at the bottom; a gentle scroll up
    detaches and stays detached through streaming. This is the specific regression that killed #448.
  - Selection survives streaming and scroll.
  - Correctness assertions hard-fail the build.
- **Frame time.** Not measured. It belongs with the render-cost work the Overview puts out of scope,
  and a budget reported against a committed baseline is worth little on shared CI hardware too noisy
  to gate on. Smoothness here comes from correct geometry, which the assertions above do cover.
- **Existing gate, untouched.** `test_e2e.py::test_hidden_tab_preserves_scroll_window` stays exactly
  as written. Its value is that it was authored independently of this rewrite — it is what caught
  #448's regression, and it must stay green.
- **Backend.** `transcript_geometry_test.py` mirroring `member_last_used_test.py`: round-trip,
  concurrent writes under the lock, malformed payload rejection, first-touch file creation.
- **Fixture.** A real tool-heavy transcript is required. Synthetic prose fixtures render at roughly
  160px/event and mask the mismatch entirely — this is why earlier repro attempts failed to
  discriminate fixed from unfixed code.

## Open questions

These are the questions as they stood when the plan was written, kept as the record of what was
uncertain going in. Several were settled by building it — see the module list above for what the
answers turned out to be.

- **Measurement hysteresis.** `MEASURE_HYSTERESIS_PX = 1` plus `getBoundingClientRect` fixed a real
  self-sustaining ~1px jitter loop (#264). TanStack's `measureElement` has no equivalent threshold.
  Does wrapping it preserve that fix, or does the library's internal remeasure path bypass the
  wrapper?
- **Two writers of `scrollTop`.** `shouldAdjustScrollPositionOnItemSizeChange` and our follow-state
  can both want to move the viewport. A gesture guard is the intended arbitration, but the
  interaction needs proving — every prior attempt failed at exactly this seam.
- **Row-boundary stability.** Skill-expansion merging and late subagent linkage can change *which
  events map to which row* retroactively, not just a row's height. Does the stored boundary set need
  versioning, or is invalidating the affected range sufficient?
- **Width bucket boundaries.** Which exact widths? Needs to stay compatible with the open mobile
  design PR (#271).
- **Server-side pruning.** Bounds on the geometry file for very long conversations, and whether the
  server prunes or only the client does.
- **Frame-time baseline portability.** A baseline recorded on CI hardware and one recorded locally
  will differ substantially. Per-environment baselines, or CI-only?
- **Phase 2 ordering.** The harness is scheduled before the swap so later phases are measurable, which
  differs from the "land it all at once, no intermediate ships" preference. Confirm this ordering is
  acceptable within the single branch.
