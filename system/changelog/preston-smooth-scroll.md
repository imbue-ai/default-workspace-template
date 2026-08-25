Marked the scroll portions of docs/system/specs/chat-scroll-and-selection-bugs.md as superseded by transcript-smooth-scroll.md: the machinery it diagnoses has been replaced by the transcript scroll engine (phase 3 deleted it).

The scroll verification driver (system/scripts/scroll_verification/verify_scroll.py) now gates FOLLOW pinning on sustained painted gaps (single-frame transients are bounded by the engine's list ResizeObserver) and its imports are sorted.
