Phase 1 of the transcript smooth-scroll rework (docs/system/specs/transcript-smooth-scroll.md): the pure scroll engine under frontend/src/models/transcriptScroll/, not yet wired into any view.

- Strict TypeScript state machines: scroll position (FOLLOW / USER_CONTROLLED with a row-key + px-offset anchor) and scrollbar interaction (ELSEWHERE / SCROLLBAR with a frozen track mapping), as pure exhaustive reducers.

- Physical-layer geometry: exact row prefix sums, anchor resolution with an exact scrollTop round-trip, and the visible-row window computation.

- Custom-scrollbar track math: the physical region maps in pixel space, virtual end regions in event-index space; live mapping, fraction-to-target resolution, and thumb placement.

- Virtual end-spacer sizing from the measured physical average with smoothing, each update paired with its exact scrollTop compensation.

- Progressive-fill planner for the physical window (instant tail page, chunked growth toward the user, jump-window replacement, cap eviction and re-centering).

- Persistence codec for the per-agent scroll state and a ring-buffer scroll trace for the ?debug=scroll instrumentation.

- Frontend CI now runs the vitest suite (npm test) alongside the build, so these and the pre-existing frontend unit tests gate PRs.
