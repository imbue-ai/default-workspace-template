Adds a **markdown preview** service. `render_markdown_preview.py` renders a
markdown file the way GitHub will -- raw HTML, centered heroes, badges, and
tables all intact -- and the `markdown-preview` service serves it as a tab you
can open with `layout.py open service:markdown-preview`.

It serves the previewed file's own directory alongside the page, so relative
images resolve; a preview that showed every local image broken would hide the
exact problem it exists to catch. The page shows the file's absolute path with
a one-click Copy path button.

The preview is on-demand, not a permanent tab: the service is never
autostarted, rendering something is what brings it up, and
`render_markdown_preview.py --close` stops it and takes the tab away again. An
idle previewer has no business sitting in your workspace.

This is what lets an agent show you a rendered README instead of pasting raw
markdown into chat -- the publish-inspiration flow now does exactly that before
shipping an inspiration's landing page, and closes it afterwards.

Repoints the broken `docs/system/style_guide.md` symlink. It targeted
`vendor/mngr/style_guide.md`, which resolves relative to `docs/system/` and so
pointed at a path left stale by the `system/` relayout; it now resolves to the
real file.

The repo-wide prose ratchets skip symlinks resolving into `system/vendor/`,
which is what they always intended -- until now that only worked because this
link was broken.
