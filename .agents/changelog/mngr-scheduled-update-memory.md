A scheduled workspace update no longer stops to ask for memory it is allowed to free.

An update's heaviest moments -- launching its background helper, its test gate, the frontend build -- are the ones the memory guard kills first, and the memory that would unblock them is usually held by the apps. The update could stop only the apps no window was showing, which on a real run freed almost nothing: the memory was in three apps with windows open, and the update waited six and a half hours for the user to say it could stop them.

A scheduled update may now stop any app the memory candidates command lists, windows open or not. That list is the boundary, drawn so everything in it can be brought back: no critical app, nothing internal, nothing sharing a critical app's program, and nothing without a program to start again. A pressed "Update now" keeps the old rule, since the user is at the screen.

`run-status started-by` tells the two apart from the record the app writes when it creates the update chat, matching on the chat's name or its id, either one -- renaming a chat renames its agent, and a scheduled run in a renamed chat would otherwise fall back to asking.

`stop-apps` records each app before it stops the next, and `resume-apps` starts them again. The record matters because the pass stopping them is the one running out of memory, so its own chat may be killed before it restores anything; the next run carries over whatever this one never started. A landed update restarts every program regardless. The pass stops programs and never windows, so a window stays where it is and its page works again once its app is back.

A background helper whose checks were killed now has the update free memory for it under that rule and report the room it made, rather than improvising. It reaches the user only when everything the update may stop is stopped and there is still not enough. Narrowing or skipping a check is never the answer -- and the helper now judges a kill by the figure the memory guard acts on, not the container's `/proc/meminfo`, which reads far more free than the guard sees and sent it straight back into another kill.

New reference: `.agents/skills/update-self/references/freeing-memory-for-an-update.md`. Every other flow still offers the candidates and stops only what the user approves.
