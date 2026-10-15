The browser pane and the browser's refusals now say what happened in plain words, with the underlying reason behind "See details".

- A crashed browser's cover says it stopped and kept its tabs and sign-ins, mentions memory only when it was killed, says "stopped responding" when it still runs, and offers "Start browser"; the picture comes back once it starts.

- When the browser program itself stops or restarts, the pane shows the browser as stopped and picks it back up when the program returns, instead of "terminated by an agent".

- A browser that cannot start yet says whether it is still being set up (try again in a minute) or could not be set up (ask an agent to fix it). Opening Browser, a local link, and the Start button each carry a sentence for people, which the desktop shows in place of the browser's own reason; a "Couldn't start the browser" refusal is marked as one to report, so the desktop can offer Report a bug.

- A refused Start shows our sentence (another browser is running, browsers still restoring, the browser didn't respond, or "Couldn't start this browser. Try again. If this persists, report a bug.") with the status and reason behind "See details". A browser that could not be set up adds "Ask an agent to fix this", which starts a chat asking an agent to fix the failed setup step, with its exit code and output; a sentence that asks for a bug report adds "Report a bug". Both appear only inside the workspace's desktop, and Report a bug only where Imbue Studio can open its bug report.

- A local link opened in a running browser no longer makes the desktop report a failure while the page is still opening.

- The memory warning says the machine this workspace runs on is low on memory, and the WebCodecs cover says to update Imbue Studio.
