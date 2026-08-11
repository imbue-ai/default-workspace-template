Replace named dockview layouts with **projects** as the way the workspace is organized.

A project is a named dockview arrangement plus the display metadata that identifies it: a name, a color, and one of ten hand-drawn squiggle glyphs. Membership is implicit — a tab is "in" a project exactly when a panel for it exists in that project's saved content — so there is no separate membership list to drift out of sync. Projects are stored per-project under `workspace_layout/projects/<id>.json` with a `projects_meta.json` registry holding the metadata and the last-active id.

The `everything` project always exists and can never be deleted. A newly created tab is added to the active project and mirrored into Everything's stored content, which is what makes Everything the unfiltered view while still letting it keep its own arrangement. Destroying a tab removes its panel from every project that holds it, so switching to a project that is not currently open no longer restores a tab whose agent, terminal, or browser is gone.

The top-left project picker switches projects, creates them, and opens a settings dialog for renaming, recoloring, and re-glyphing one (or deleting it — Everything's delete is permanently disabled and says why). A narrow icon rail down the left edge expands on hover and holds the active project's squiggle and name, quick-add rows for the four tab types (chat, file viewer, browser, terminal), and the machine's apps. A separate all-apps picker lists every app on the machine without filtering against the current project.

Every tab's header menu now offers, in order: Refresh, Open in new window, Share, Destroy, and Close. Destroy is confirm-gated and states that the tab is removed from all projects and that the transcript stays accessible.

New API endpoints back all of this: `GET`/`POST /api/projects`, `GET`/`POST /api/projects/<id>`, `POST /api/projects/<id>/settings`, `POST /api/projects/<id>/delete`, and `POST /api/projects/panels/<panel_id>/delete`. Saves, deletes, and metadata edits broadcast over the existing WebSocket so other connected clients re-apply or fall back.

Agent-driven layout ops (`system/scripts/layout.py`) keep working against projects: a connected client now reports its active project as its active layout, so `--layout <project name>` resolves through the projects registry when the name is not one of the named layouts, and `inspect` / `list` read that project's saved content. Naming an unknown layout now reports the known projects alongside the known layouts.
