Transcript smooth-scroll rework, phase 3 of docs/system/specs/transcript-smooth-scroll.md (phases 1 and 2 landed the engine and the ChatPanel migration).

SubagentView runs on the same scroll engine with an empty virtual layer: its whole transcript is loaded, so the custom scrollbar is 100% pixel-space and the fill planner idles. One scrolling mechanism now serves all transcript viewing.

The old machinery is deleted: the shared scroll controller (transcript-scroll.ts), the virtual-window math (virtualWindow.ts), the follow-state decision (scrollFollow.ts), the mounted-row measurer (row-measurement.ts), and the disjoint pinned-run selection mechanism (selection facts consolidated in scroll-selection.ts; row estimates and the segment type live in conversation-rows.ts). The phantom-spacer estimate geometry -- the root cause of the scroll-jump bug -- is gone entirely.

A ResizeObserver on the message list catches content resizing outside any redraw (image loads, font swaps) so positioning corrects on the next frame, closing a one-frame FOLLOW gap flash during initial fill.

Not yet manually verified: the subagent view click-through itself (the standalone fixture carries no subagent session linkage); it shares the verified engine and render path, but should be eyeballed in a real minds workspace. Possible follow-ups noted in the spec: a "jump to latest" pill and a thumb-drag position tooltip.
