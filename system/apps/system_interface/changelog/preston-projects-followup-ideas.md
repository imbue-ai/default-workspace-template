Follow-up work on projects, from a design review of the shipped build and a pass over the object verbs.

**One verb set, wherever you meet an object.** The rail row menu and the dock tab menu now render the same definition, reached by right-click as well as by the kebab. Seven differently-named items collapse to five verbs: Refresh, Share, Rename, Hide tab, and Quit. "Remove from project" and "Delete from this machine" are gone — the first moved to project settings, the second was the same act as Quit under another name.

**Refresh works on terminals**, where it means reattaching the tmux session. The session outlives the panel, so a refresh keeps its scrollback.

**Rename stays a chat-only verb.** A chat is an mngr agent: its ref is a stable agent id and `mngr rename` moves the name everywhere the agent is known, so the name you give it is the name anything else — an agent included — can refer to it by. Nothing else on the machine manages that. A terminal is filed under its live tmux session name and a browser under a Chromium profile directory, so for those the name is the identity and a rename could only be a display name laid over the top. An app has a perfectly good stable id in its registered service name, but that name is also the only handle anything else accepts: `layout.py` expands a bare word to `service:<word>`, so an agent asked to open an app by the name you gave it would look up a service that does not exist. A name you can read but cannot then refer to is worse than no name, so all three keep their registered or derived names.

**An app's chosen name now reaches its Share verb.** An app can still carry a display name set by an agent through `layout.py rename`, and every surface but this one already showed it — a titled app read "Share web" directly above "Quit Docs" in its own menu. The share is still keyed by the service name, which is the part that has to stay stable.

**Quit is the single destructive verb** for all four kinds, and is withheld for the primary agent, which runs the workspace's own services. It stays deliberately weaker for an app: the app leaves the registry and every project, but the program answering on its port keeps serving.

**Deleting a project deletes only the view.** Nothing is shut down. Every object it showed keeps running and stays in Everything and in any other project already showing it. The guard refusing to delete the last project is gone with it — a machine may sit at zero projects and fall back to Everything.

**Project settings holds the member list**, which is where an object is removed from a project. Removal there is non-destructive in the same way: it drops the membership only.

**Pinned apps leave the All apps popover**, since they are already in the rail a few pixels away. A pinned row carries its own pin icon, so unpinning is one click without opening a menu, and a just-pinned row fades out rather than vanishing under the pointer.

**Ad-hoc pages are no longer filed as project members.** A panel with no agent, tmux session, or service was being filed under a hash of its own panel id — an identity that could not outlive the panel, and one no verb could act on.

**Design review.** An open menu now holds the rail open and sits over a scrim, so the click that dismisses it no longer also lands on whatever was underneath. The project picker is narrower, aligns its rows with the rail's, and marks the active project with a checkmark that becomes a pencil on hover instead of a background fill. The rail gets a coherent type scale and tighter icon spacing, "New project" goes primary on hover, the project settings tooltip reads "Edit <name>", and clicking a row for something already open collapses the rail and flashes the tab it focused. Hovering an inactive tab recolors its icon and title rather than emboldening them. Overflowing launcher tile labels ellipsize instead of wrapping to a second line. Tooltips can now be placed beside their anchor rather than beneath it, so a rail tooltip need not cover the row below it.

**`POST /api/projects/<id>/members`** no longer answers 500 when no primary agent is configured, matching what the read side of the same resource already did.

**A rename that runs out of time now says so.** The workspace caps the `mngr rename` it shells out to, and a capped subprocess comes back as a negative return code — so its own timeout surfaced as "rename exited with code -15", a number that says nothing about what happened or what to do next. It now names the timeout, and deliberately does not claim the rename did not happen: the subprocess is stopped partway through work that spans the provider's stored data and a live tmux session, so which half landed is not knowable from here.

**Renaming shows the new name at once.** Renaming a chat goes all the way out to the `mngr` CLI, whose startup alone is several seconds, so waiting for the round trip meant typing a name and watching the old one sit there with no sign anything had happened. What you typed is now what you see immediately; if the server refuses, the old name comes back and the message says which name it is still called, rather than only why the attempt failed.
