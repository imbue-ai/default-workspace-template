Prevent context compaction events from breaking conversation turn grouping and duplicating task progress.

- **Non-boundary classification:** Classified status messages with `boundary: false` and `isTurn: false` so context compaction notices no longer start new turns or increment turn counts.
- **Turn grouping and task continuity:** Kept compaction status events within their active turn section, preventing turn splitting and avoiding task carryover logic that re-rendered open and pending steps.
- **Rendering placement:** Positioned post-reply compaction events neatly under trailing replies via trailing status, while rendering mid-turn compaction inline chronologically.
