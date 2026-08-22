Follow-up work on projects, from a design review of the shipped build and a pass over the object verbs.

**One verb set, wherever you meet an object.** The rail row menu and the dock tab menu now render the same definition, reached by right-click as well as by the kebab. Seven differently-named items collapse to six verbs: Refresh, Share, Rename, Hide tab, Remove from project, and Quit. "Delete from this machine" is gone — it was the same act as Quit under another name. The order the last three read in is from dropping the panel to dropping the filing to dropping the object; which surface offers which of them is settled further down. "Remove from project" is absent in Everything, which is the home an object leaves only by being destroyed.

**Refresh works on terminals**, where it means reattaching the tmux session. The session outlives the panel, so a refresh keeps its scrollback.

**Rename stays a chat-only verb.** A chat is an mngr agent: its ref is a stable agent id and `mngr rename` moves the name everywhere the agent is known, so the name you give it is the name anything else — an agent included — can refer to it by. Nothing else on the machine manages that. A terminal is filed under its live tmux session name and a browser under a Chromium profile directory, so for those the name is the identity and a rename could only be a display name laid over the top. An app has a perfectly good stable id in its registered service name, but that name is also the only handle anything else accepts: `layout.py` expands a bare word to `service:<word>`, so an agent asked to open an app by the name you gave it would look up a service that does not exist. A name you can read but cannot then refer to is worse than no name, so all three keep their registered or derived names.

**An app's chosen name now reaches its Share verb.** An app can still carry a display name set by an agent through `layout.py rename`, and every surface but this one already showed it — a titled app read "Share web" directly above "Quit Docs" in its own menu. The share is still keyed by the service name, which is the part that has to stay stable.

**Quit is the single destructive verb** for all four kinds, and is withheld for the primary agent, which runs the workspace's own services. It stays deliberately weaker for an app: the app leaves the registry and every project, but the program answering on its port keeps serving.

**Deleting a project deletes only the view.** Nothing is shut down. Every object it showed keeps running and stays in Everything and in any other project already showing it. The guard refusing to delete the last project is gone with it — a machine may sit at zero projects and fall back to Everything.

**Every rail row removes in one click.** A pinned app already had its pushpin; a chat, terminal or browser row now has the same one-click affordance for the same act, beside the menu that also carries it. It is absent in Everything, which is the home an object leaves only by being destroyed.

**Pinned apps leave the All apps popover**, since they are already in the rail a few pixels away. A pinned row carries its own pin icon, so unpinning is one click without opening a menu, and a just-pinned row fades out rather than vanishing under the pointer.

**Ad-hoc pages are no longer filed as project members.** A panel with no agent, tmux session, or service was being filed under a hash of its own panel id — an identity that could not outlive the panel, and one no verb could act on.

**Design review.** An open menu now holds the rail open and sits over a scrim, so the click that dismisses it no longer also lands on whatever was underneath. The project picker is narrower, aligns its rows with the rail's, and marks the active project with a checkmark that becomes a pencil on hover instead of a background fill. The rail gets a coherent type scale and tighter icon spacing, "New project" goes primary on hover, the project settings tooltip reads "Edit <name>", and clicking a row for something already open collapses the rail and flashes the tab it focused. Hovering an inactive tab recolors its icon and title rather than emboldening them. Overflowing launcher tile labels ellipsize instead of wrapping to a second line. Tooltips can now be placed beside their anchor rather than beneath it, so a rail tooltip need not cover the row below it.

**`POST /api/projects/<id>/members`** no longer answers 500 when no primary agent is configured, matching what the read side of the same resource already did.

**A rename that runs out of time now says so.** The workspace caps the `mngr rename` it shells out to, and a capped subprocess comes back as a negative return code — so its own timeout surfaced as "rename exited with code -15", a number that says nothing about what happened or what to do next. It now names the timeout, and deliberately does not claim the rename did not happen: the subprocess is stopped partway through work that spans the provider's stored data and a live tmux session, so which half landed is not knowable from here.

**Renaming shows the new name at once.** Renaming a chat goes all the way out to the `mngr` CLI, whose startup alone is several seconds, so waiting for the round trip meant typing a name and watching the old one sit there with no sign anything had happened. What you typed is now what you see immediately; if the server refuses, the old name comes back and the message says which name it is still called, rather than only why the attempt failed.

**The frontend test suite no longer runs twice.** `npm run build` type-checked with `tsc` in a mode that also emitted compiled JavaScript into `frontend/dist/` — a directory nothing reads, since the bundle goes elsewhere. Vitest collected the compiled copy of every test alongside its source, so any checkout that had run a build reported the same suite twice over, with one half frozen at whenever that build last ran. The type check now emits nothing, and `dist/` is excluded from test collection so a directory left behind by an older build cannot do this again.

**The rail folds up when the window loses focus**, not only when the pointer leaves it, so clicking away no longer leaves a 240px card floating over the dock. An open menu or a row mid-rename still holds it open, exactly as they do for an ordinary pointer-leave.

One gap is known and deliberate: in the desktop app the rail does not fold when the cursor merely slides off the window without a click. The workspace is a cross-origin frame inside the minds chrome, and a cursor leaving the app window raises no event it can hear — a page can only learn the pointer's whereabouts from events, and none arrive. Closing it would take a signal relayed from outside the frame, which was judged more machinery than a rail collapse is worth. Served straight to a browser, where those events do arrive, it folds on the cursor leaving too.

**Opening something already open keeps the launcher up, for real this time.** The launcher retired itself explicitly the moment anything opened, which is a separate path from the one that folds launchers away when another panel takes focus — so an earlier fix to the latter never reached it. Both paths now tell a reveal apart from a creation, and only a creation takes the list away.

**The attention flash is a flash.** Reopening something already open used to fade the tab out and back, which read as the tab leaving rather than arriving. It is now a brief amber wash — a hue this workspace uses for nothing else, so it cannot be mistaken for a state the tab has entered and stayed in — and it holds once instead of pulsing under reduced motion.

**Starting something new says so, and cannot be started twice.** `mngr create` takes seconds, and the launcher used to sit there unchanged for all of it: the click looked like it had missed, and clicking again started a second object. The tiles now stand down and the heading reads "Starting…" until the object exists, whether it arrives or fails.

**Project settings is display metadata only** — name, colour, squiggle, and the delete button. Removing an object from a project is a verb on the object, so it lives in the object's own menu on both surfaces.

**A workspace that is not answering says so.** A 502, 503 or 504 comes from the tunnel in front of the workspace, not from the workspace itself: the request reached no endpoint and nothing changed. That used to surface as a bare "HTTP 503" on whatever had just been clicked, which read as that feature being broken rather than the workspace being briefly away.

**The rail's four built-in rows can be put away, per project.** Chat, File Viewer, Browser and Terminal now carry the same pin affordance a pinned app does: unpin one and it moves into the All apps menu, pin it back and it returns to the rail. What each row does is unchanged — this moves where it is offered, nothing else.

Which starting points a project keeps to hand belongs to that project, so it is stored per project rather than per user, and recorded as the rows taken out rather than the rows kept: a project that has never touched this shows all four, which is every project until it says otherwise. Everything has no project entry to record against and always shows the full set.

Unlike an app, none of these four is an object with a member ref — a chat is a create, and the terminal and browser services are fleets reached by making a session rather than by opening the service — so this rides its own field rather than the member list.

**Hide and unfile each live where they belong.** The tab's menu keeps "Hide tab" and no longer offers "Remove from project"; the rail's row menu is the other way round. Putting a tab away is what you want while looking at the tab, and taking an object out of a project is what you want while looking at the project's list of what it shows.

**The tab's hide control is an X**, and sits outboard of the three dots rather than inboard, which is where a close lives in every other tabbed thing.

**The "+" goes while a New Tab tab is open.** A pane holds at most one, so with one on screen the button could only focus the tab already in front of you.

**An app appears once in the rail.** Pinned it is a shortcut row, unpinned it is an All apps row; either way the list below no longer repeats it. That row carries the app's own menu too, so its verbs did not go with the duplicate. The one exception is an app the machine no longer offers: it has no shortcut to draw, so it keeps its place in the list, where it can still be taken out of the project.

**Opening something already open from the New Tab view no longer moves anything.** The tab holding it flashes to say which one it is; focus stays where it was, and the list you were reading stays in front of you.
