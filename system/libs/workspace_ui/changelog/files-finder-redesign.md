The shared icon set gains the glyphs Files draws its chrome with: layout-grid, list, columns, plus, upload, ellipsis, folder, house, inbox, file-plus, copy, info, panel-left, link, and laptop-off (a laptop struck through, for a computer that is not answering).

`alertDotIcon()` is a filled disc with a white exclamation in the current colour.

A hover tooltip can ask for a narrower bubble (`hoverTooltipAttrs(text, placement, { maxWidth })`), so a sentence or two of explanation wraps instead of running out in one long line.

The type scale gains its smallest step, `--font-size-tag` (10px), and a `type-tag` role on it, for a word badge set beside other text such as "Read-only".

The shell's toasts move here, as `models/toasts.ts` (the queue) and `components/toasts.ts` (the stack), so Files shows its notes with them too. A page may pass `onDismiss` to give each toast a close button; the shell passes none, so its toasts still take no press.

`SegmentedControl` is a few choices side by side in one outlined well, the chosen one filled, each a glyph or a label. It is a radio group, moved along with the arrow keys. Files' view switch is the first.

The `folder` glyph is redrawn in the Files app icon's hand-drawn shape, and a matching hand-drawn `page` glyph joins it.
