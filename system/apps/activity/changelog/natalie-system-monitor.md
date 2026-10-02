New built-in app, System Monitor (the `activity` package): shows what is using the workspace's memory, in terms a non-technical user recognizes, with the technical detail one click away.

- A plain headline (room to spare, getting tight, about to start closing things) over the workspace's real limit, read from the host on cloud workspaces and from the container's cgroup otherwise, measured against where closing actually starts (earlyoom's own threshold, or the kernel's limit when that comes first).

- Memory grouped by chat (every harness: Claude, Codex, Pi, OpenCode, Antigravity), app, and background service. A chat can be stopped from here through the chat app's own stop; it picks up where it left off when next messaged. A stop refuses to interrupt a chat that started working since the page last looked, unless you confirm.

- Every figure opens to the processes it is made of, their command lines and shedding priority, and which process the memory guard would most likely close first.

- Questions a newcomer would ask (what happens if memory fills up, how to free it, whether you can get more) are answered on the page, picked for what it is showing. "Ask in chat" drafts the question, with the page's numbers, into your current chat without sending it, so it starts no new chat; the README tells the answering agent where the live figures are.

- Each section and its rows share their slice of the bar's colour, and a legend entry jumps to its section. Each row's bar and percentage are its share of the memory in use. Text and bar colours meet WCAG AA contrast, checked by a test against the design tokens.

- It runs only while a window shows it: the shell stops it a minute after its last window closes, and the page reads nothing while its window or browser tab is out of sight. The API is the workspace owner's only, and a write must come from the app's own page.
