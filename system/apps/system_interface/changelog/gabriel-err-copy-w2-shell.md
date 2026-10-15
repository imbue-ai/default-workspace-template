The workspace desktop says what failed in its own words, names apps as people know them, and offers the next step.

- Toasts and dialog lines name what failed ("Docs couldn't be opened.", "This window couldn't be closed.", "Couldn't move this shortcut, so it's back where it was."). A failure with no reason of its own ends "Try again. If this persists, report a bug." and offers "Report a bug" where Imbue Studio opens one; the shell's reason sits behind "See details". Apps are named by their display name everywhere, and a launch says "<App> couldn't open that."

- The shell's refusals carry a sentence for people (`user_facing_message`, with `reportable` where it asks for a bug report): an app that is no longer in the workspace, a launch path it no longer offers, a window or desktop that has gone, the last desktop's delete, and a change that could not be saved (a full disk says so and offers an agent to free up space, and every failed save offers Try again). A failed launch carries no sentence of the shell's: the desktop words it.

- The launcher and the phone's start sheet keep the typed text until a row runs, and show a failure under the field ("Couldn't send that to Chat, so your text is still in the box."). Text sent into Chat that is refused leaves Chat's window where it was.

- A failed startup read is told in the backdrop with Reload, and stays until a read succeeds.

- A stopped app's window says it isn't running and offers to ask an agent to start it (or to report a bug, for Chat); its shortcut and launcher row say the same. A window whose app is gone says so and offers to close it.

- Delete is withheld from the only desktop, saying why. The avatar chooser retries its designs in place. The update notice says how long ago the update was, names what a rollback restarts by display name, and offers an agent to finish a rollback that left the restart to one.

- A stopped app's page says "Starting <name>..." without guessing a cause; one that couldn't start waits for Try again instead of reloading on its own, offers an agent to look at it, and keeps the log command behind "See details".

- The "interface needs to be rebuilt" page offers "Ask an agent to fix this", which starts the agent and shows its chat; the command and a working terminal session are behind "See details". A page load that fails shows a page of ours with Try again instead of raw JSON.
