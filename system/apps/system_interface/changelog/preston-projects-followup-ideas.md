Follow-up work on projects, from a design review of the shipped build and a pass over the object verbs.

**One verb set, wherever you meet an object.** The rail row menu and the dock tab menu now render the same definition, reached by right-click as well as by the kebab. Seven differently-named items collapse to five verbs: Refresh, Share, Rename, Hide tab, and Quit. "Remove from project" and "Delete from this machine" are gone — the first moved to project settings, the second was the same act as Quit under another name.

**Refresh works on terminals**, where it means reattaching the tmux session. The session outlives the panel, so a refresh keeps its scrollback.

**Rename now covers apps as well as chats, and is withheld from terminals and browsers.** A chat's ref is its stable agent id and an app's is its registered service name, so a chosen name is free to move and does. A terminal and a browser have no identity apart from their names — a terminal is filed under its tmux session name, a browser under a Chromium profile directory — so a rename there could only ever be a display name laid over the top, leaving `tmux ls`, the profile on disk, and every stored reference still saying the old one. Rather than offer a rename that stops at the surface, both keep their derived numbering. Renaming needs no open tab, and a chat's rename also moves the agent's own canonical name.

An app's rename now reaches every surface, including its Share verb. The registered service name is the app's stable id — it keys the member ref, `apps.toml` and the supervisord program — and the share is still keyed by it, but it no longer surfaces: a renamed app used to read "Share web" directly above "Quit Docs" in its own menu.

**Quit is the single destructive verb** for all four kinds, and is withheld for the primary agent, which runs the workspace's own services. It stays deliberately weaker for an app: the app leaves the registry and every project, but the program answering on its port keeps serving.

**Deleting a project deletes only the view.** Nothing is shut down. Every object it showed keeps running and stays in Everything and in any other project already showing it. The guard refusing to delete the last project is gone with it — a machine may sit at zero projects and fall back to Everything.

**Project settings holds the member list**, which is where an object is removed from a project. Removal there is non-destructive in the same way: it drops the membership only.

**Pinned apps leave the All apps popover**, since they are already in the rail a few pixels away. A pinned row carries its own pin icon, so unpinning is one click without opening a menu, and a just-pinned row fades out rather than vanishing under the pointer.

**Ad-hoc pages are no longer filed as project members.** A panel with no agent, tmux session, or service was being filed under a hash of its own panel id — an identity that could not outlive the panel, and one no verb could act on.

**Design review.** An open menu now holds the rail open and sits over a scrim, so the click that dismisses it no longer also lands on whatever was underneath. The project picker is narrower, aligns its rows with the rail's, and marks the active project with a checkmark that becomes a pencil on hover instead of a background fill. The rail gets a coherent type scale and tighter icon spacing, "New project" goes primary on hover, the project settings tooltip reads "Edit <name>", and clicking a row for something already open collapses the rail and flashes the tab it focused. Hovering an inactive tab recolors its icon and title rather than emboldening them. Overflowing launcher tile labels ellipsize instead of wrapping to a second line. Tooltips can now be placed beside their anchor rather than beneath it, so a rail tooltip need not cover the row below it.

**`POST /api/projects/<id>/members`** no longer answers 500 when no primary agent is configured, matching what the read side of the same resource already did.
