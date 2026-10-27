A scheduled workspace update no longer stops to ask for memory it is allowed to free.

An update's heaviest moments -- launching its background helper, its test gate, the frontend build -- are the ones the memory guard kills first, and on a workspace with many apps running the memory that would unblock them is held by the apps themselves. Until now the update could stop only the apps no window was showing, which on a real run freed almost nothing: the memory was in three apps with windows open, and the update waited six and a half hours for the user to say it could stop them.

A scheduled update may now stop any app the memory candidates command lists, windows open or not. That list is the boundary, and it is drawn so everything in it can be brought back: no critical app (the shell, the chat and the terminal), nothing internal, and nothing without a program to start again. An update the user pressed "Update now" on keeps the old rule, since they are at the screen and an app going dark under them is a surprise. The update tells the two apart from the record the app writes when it creates the update chat.

The pass stops the program and never the windows, so a window stays where it is and its page works again once the app is back. A landed update restarts every program on its own; one that ends any other way starts back by name what it stopped, and the results message names each app and says it is running again.

A background helper whose checks were killed for memory is now a case the update answers itself, rather than one it improvises: it frees memory under that rule and replies with the room it made, measured against the figure the memory guard acts on. Narrowing or skipping a check is still never the answer. It reaches the user only when everything the update may stop is stopped and the room is still not there.

New reference: `.agents/skills/update-self/references/freeing-memory-for-an-update.md`. The general rule for every other flow is unchanged -- it offers the candidates and stops only what the user approves -- and now says so explicitly.
