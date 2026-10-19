The browser moved onto the Screen (`docs/system/specs/screen/spec.md`, sections 16 and 17):

- `agentic-browser-fleet` keeps its verbs and exit codes against the Screen service, and its skill says what changed: the browser runs on the Screen beside everything else the user runs there and no longer stops when its window closes; `new` opens the Screen's window minimized as before and `handoff` shows it; the lease is the Screen's, so a human using the Screen holds the browser too and `ls` says "human (driving the Screen)"; `screen shot` shows whatever is frontmost, so an agent raises Chromium's window with `screen activate` before relying on it; after a crash `new` restarts the same browser and the agent attaches under a fresh `playwright-cli` slug.

- A new `use-screen` skill documents the `screen` command (`run`, `windows`, `activate`, `shot`, `size`, `control acquire|release|handoff`, `open`), the lease rules with the human always winning, and xdotool on `DISPLAY=:99` and the CDP proxy as the two input paths.

- `manage-desktop` names the Screen where it named the browser (the `screen` app's `browser` launch path behind `open https://...`, the Screen's window, the Screen service's stop and start routes), and `build-app` lists the Screen's ports (8083, 8090) among the ones to avoid.

- The `use-screen` skill lists `screen browser open [URL]`, the browser launch path as a verb of the `screen` command.
