The workspace desktop says what failed in its own words, names apps as people know them, and offers the next step.

- Toasts and dialog lines name what failed ("Docs couldn't be opened.", "This window couldn't be closed.", "Couldn't move this shortcut, so it's back where it was."). A failure with no reason of its own ends "Try again. If this persists, report a bug.", offers Try again where pressing again could help, and offers "Report a bug" wherever Imbue Studio opens one by the time it is drawn, with the failure attached; the shell's reason sits behind "See details". Apps are named by their display name everywhere, and a launch says "<App> couldn't open that."

- The shell's refusals carry a sentence for people (`user_facing_message`, with `reportable` where it asks for a bug report): an app that is no longer in the workspace, a launch path it no longer offers, a window or desktop that has gone, the last desktop's delete, and a change that could not be saved (a full disk says so and offers an agent to free up space, and every failed save offers Try again). A failed launch otherwise carries the app's own words, and the desktop words it.

- A launch into an app that isn't running says what the app's window would ("Terminal is not running right now." with "Ask an agent to start it"; the note goes once the app runs again), and a launch that woke a stopped app that didn't come up says what its could-not-start page says (couldn't start, failed several times so not started again, or still starting).

- Once an app has taken a launch, a failed write of where its window is no longer fails the launch: the window shows the page, the launcher lets go of the text, and a note says the window's place wasn't saved (on a full disk, with an agent to free up space). When no window could be opened for it, the note says so (on a full disk, with an agent to free up space) and offers no Try again, which would ask the app a second time.

- The launcher and the phone's start sheet keep the typed text until a row runs, and show a failure under the field ("Couldn't send that to Chat, so your text is still in the box."). Text sent into Chat that is refused leaves Chat's window where it was.

- A failed startup read is told in the backdrop with Reload, and stays until a read succeeds.

- A stopped app's window says it isn't running and offers to ask an agent to start it (or to report a bug, for Chat); its shortcut and launcher row say the same. A window whose app is gone says so and offers to close it.

- "Ask an agent to start it", "Ask an agent to look at it" and "Ask an agent to finish it" start a new chat, and leave their note up; "Ask an agent to free up space" goes to the main chat. Pressed, each (and the rebuild page's "Ask an agent to fix this") reads "Asking an agent…" and takes no second press until the agent's chat is up; one that fails comes back, with the failure told.

- Delete is withheld from the only desktop in the overview, saying why. The wallpaper and theme choosers, the resource gauge's settings and the keyboard shortcuts dialog say what couldn't be changed (or loaded) with the reason behind "See details", and offer Try again (and, on a full disk, an agent to free up space). The theme gallery page says it couldn't load, with the reason behind "See details" and Try again, and Report a bug where a shell framing it opens one (elsewhere its sentence asks only to try again). The avatar chooser retries its designs in place. The update notice says how long ago the update was, names what a rollback restarts by display name, and offers an agent to finish a rollback that left the restart to one.

- A stopped app's page says "Starting <name>..." without guessing a cause; one that couldn't start waits for Try again instead of reloading on its own, offers an agent to look at it (a refused ask told under it, with "Report a bug" where it asks for one and the shell can open one), and keeps the log command behind "See details".

- The "interface needs to be rebuilt" page offers "Ask an agent to fix this", which starts a new chat and shows it; the command, a working terminal session (made only when its own section is opened) and, after a refused attempt, its reason are behind the page's one "See details". A page load that fails shows a page of ours with Try again instead of raw JSON.
