# Share button deep link (workspace side)

Clicking Share on an app tab's menu or a sidebar app row now asks the
embedding minds chrome (via the embed contract's new
`minds:open-share-settings` type) to open its Share page focused on that app,
replacing the instructional popup that told the user where to go. The popup
(`ShareModal`) and its styles are deleted.

The deep link takes effect with the next minds release: until the vendored
contract sync lands, an older minds chrome ignores the message and the click
is a no-op, per the contract's tolerant policy. Opened directly in a browser
(no embedder), the click is likewise a no-op, matching the permission card's
"Review & respond" button.

The tab and rail object menus are reordered into an acting group (Refresh,
Share, Add to project) and a removal group in increasing severity (Close tab
-- previously labeled "Hide tab" -- Remove from project, Stop, Delete). An
app instance's service verbs now sit in those ordinary positions instead of a
trailing group. Share rows use the lucide user-plus icon, matching the minds
shell's sharing surfaces.
