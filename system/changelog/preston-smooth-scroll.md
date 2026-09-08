Marked the scroll portions of docs/system/specs/chat-scroll-and-selection-bugs.md as superseded by transcript-smooth-scroll.md: the machinery it diagnoses has been replaced by the transcript scroll engine (phase 3 deleted it).

The scroll verification driver (system/scripts/scroll_verification/verify_scroll.py) now gates FOLLOW pinning on sustained painted gaps (single-frame transients are bounded by the engine's list ResizeObserver) and its imports are sorted.

CI now runs the frontend test suite on Node 22 (was 20, which is past end-of-life and lacks the global `navigator` that Projects.test.ts exercises -- the suite's six failures on this branch's PRs were all Node-20-only).

The scroll verification driver waits out the engine's persistence debounce before clearing storage between phases, and polls for the restore trace record instead of assuming a fixed reload settle time, so both checks hold on transcripts that have grown past 15k events.

The scroll verification suite's A1 check is now cap-aware: it passes when the whole transcript is physical, or when a transcript larger than PHYSICAL_CAP_EVENTS converges to a cap-sized window at the live tail with no fill in flight (the engine exposes capEvents in its debug state). All 22 checks pass against a 6.8k-event fixture with a 5k cap.
