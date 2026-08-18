Fix the chat transcript jumping while reading history: the view no longer yanks the reader to the bottom (or an arbitrary position) when older pages load, turns regroup, or estimated row heights settle.

Two changes, replacing the earlier one-shot backfill anchor:

- Tail-following now re-arms only on an actual downward scroll into the bottom band. Position alone never re-arms it, so a page landing that clamps the reader to the bottom (followed by a native scroll-anchoring adjustment) can no longer flip the chat back into follow mode and hard-snap the reader to the tail.

- While the reader is scrolled up, their position is derived state: the row at the top of the viewport (with a pixel offset) is captured on every user scroll, and every redraw whose geometry moved that anchor applies the difference to the live scrollTop. This compensates page landings, turn regrouping, and estimate-to-measured settling in the frame they happen, instead of relying on native scroll anchoring, which silently gives up whenever a landing shifts the window mapping by more than the overscan and the browser's anchor node unmounts. Native anchoring is disabled on the chat scroll container so the two mechanisms cannot double-correct. While older history remains above, the anchor skips the boundary row that absorbs backfilled events, so pages folding into a long-running turn no longer drag the reader with them.

Verified against a real 899-event tool-heavy transcript with a Playwright wheel-input harness: previously every 50-event backfill displaced the reader by 25 to 126 events (sometimes to the very bottom); with this change there are no forward displacements, no tail snaps, and scrolling toward the tail after a jump proceeds at exactly the wheeled distance.
