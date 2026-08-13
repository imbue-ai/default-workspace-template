# atlas-book

A sidebar viewer for the Atlas project book (a tab).

The left sidebar lists Atlas one-pagers grouped by **project**, each **feature**
with a colored state dot, its live §0 status line, and a **lifecycle badge**
(proposed / active / paused / shipped / abandoned) so the status is visible
without opening the page. The main pane opens in **Overview** mode -- a plain,
high-level view (what it does / what it supports today / what's next) derived from
the page's own sections with the machine status line and citations stripped -- with
a one-click toggle to **Technical** mode (the full one-pager: all sections plus the
linked Sources footnotes). It also offers a status picker to set the topic's
lifecycle status. Clicking a feature swaps the pane without a full reload; a 30s
poll refreshes the sidebar statuses and the open page.

It reads the repo's `atlas/` files directly (so it is always current) and reuses
the atlas skill's own logic -- `atlas_index.gather()` for the project->feature
tree, `atlas_status.build_status_line()` for each feature's live status, and
markdown-it for rendering. Only known topic slugs render (unknown -> 404). The one
write it allows is `POST /topic/<slug>/status`, which rewrites just the `status`
line in the topic's declaration (slug allow-listed, status allow-listed, result
verified before the write).

Runs as the `atlas-book` supervisord program on its own origin.
