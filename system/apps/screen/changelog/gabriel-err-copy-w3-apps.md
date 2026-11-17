The Screen says what happened in plain words when it cannot run, with the service's own text behind "See details", instead of showing internal reasons like "screen-neko exited unexpectedly" on the window.

- While the Screen is first installed it says "The Screen is still being set up in this workspace." and starts on its own once ready. If the install failed in this boot, it says "The Screen couldn't be set up in this workspace." with "Ask an agent to fix this", carrying the install step's exit code and error output; before, it said "Installing the screen" forever.

- A stopped Screen says why: "The Screen was stopped." (with Start) after Quit; "The Screen stopped unexpectedly." (with Start, "Ask an agent to fix this" and Report a bug) after a crash, adding that the workspace ran low on memory when the memory guard ended it; and "Couldn't start the Screen. Try again. If this persists, report a bug." (with Try again, an agent and Report a bug) when its start failed. A refused Start now says so on the window instead of only in the console.

- A viewer that could not load the Screen's state says "The Screen couldn't load." on the window (before, the failure went only to the console), with "The workspace didn't answer. Try again in a moment." when nothing answered, and a Try again that loads it again. Until a state arrives, the viewer no longer shows the agent's "Take control" dim or the strip's Take control and Give control back buttons.

- Opening Browser or a link while the browser is still being set up, after its setup failed, with too many browsers open, or when the Screen's window could not be shown, now shows a sentence of ours on the desktop, with the reason behind "See details".

- Report a bug appears only where Imbue Studio can open its bug report, and carries the overlay's sentence and what its "See details" holds. "Ask an agent to fix this" appears only inside the workspace's desktop, and starts a new chat.
