The shared workspace UI library gains the pieces the chat transcript's restyle needs. `system/libs/workspace_ui/src/base.css` adds the `--width-agent-gutter` token (how far the agent's side of a conversation stays clear of the column's right edge), the `--c-step-done` and `--c-step-pending` progress-bullet colours with their `text-step-done` / `text-step-pending` utilities, and a `--spinner-width` knob on the shared ring spinner. `system/libs/workspace_ui/src/components/icons.ts` adds the small tool glyphs the chat's inline tool chips draw (terminal, sparkle, bot, globe, wrench) and repaints the progress status badges from the new tokens.

`system/scripts/scroll_verification/verify_scroll.py` expands an inline tool chip where it used to expand a full-width tool-call block, matching the chat's new tool-call rendering.

`.DS_Store` is now gitignored, and the stray `.agents/.DS_Store` that had been tracked is removed.
