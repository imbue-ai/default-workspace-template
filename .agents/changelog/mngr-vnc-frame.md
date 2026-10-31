The browser moved onto the Screen (`docs/system/specs/screen/spec.md`, sections 16 and 17):

- `agentic-browser-fleet` keeps its verbs and exit codes against the Screen service, and its skill says what changed: the browser runs on the Screen beside everything else the user runs there and no longer stops when its window closes; `new` opens the Screen's window minimized as before and `handoff` shows it; the lease is the Screen's, so a human using the Screen holds the browser too and `ls` says "human (driving the Screen)"; `screen shot` shows whatever is frontmost, so an agent raises Chromium's window with `screen activate` before relying on it; after a crash `new` restarts the same browser and the agent attaches under a fresh `playwright-cli` slug.

- A new `use-screen` skill documents the `screen` command (`run`, `windows`, `activate`, `shot`, `size`, `control acquire|release|handoff`, `open`), the lease rules with the human always winning, and xdotool on `DISPLAY=:99` and the CDP proxy as the two input paths.

- `manage-desktop` names the Screen where it named the browser (the `screen` app's `browser` launch path behind `open https://...`, the Screen's window, the Screen service's stop and start routes), and `build-app` lists the Screen's ports (8083, 8090) among the ones to avoid.

- The `use-screen` skill lists `screen browser open [URL]`, the browser launch path as a verb of the `screen` command.

- `migrate-workspace`'s legacy path map sends the old `libs/browser/` to `system/apps/screen/`, where the browser lives now, rather than to the deleted `system/apps/browser/`.

- The `use-screen` and `agentic-browser-fleet` skills describe `handoff` as the lease does it: the Screen goes to the human pinned (no other agent can take it until they give it back), with the agent first in line to resume; the human's first input claims it, and their "Give control back" or leaving hands it to the agent.

The update apply (`update-self`) carries a workspace that ran the browser app onto the Screen:

- The old `browser` uv tool goes the way every per-app tool install does under the fresh-venv apply: `tool_env.py drop-app-tools` removes the tool environments older builds installed editable from an app directory, console scripts included, and the Screen runs from the workspace venv, so nothing is installed over the `agentic-browser-fleet` script the old tool owned.

- The Screen's viewer is one of the frontend bundles the apply builds, installs from the worker (`--worker-bundle screen=<path>`), snapshots and verifies, beside the shell's, the chat's, Getting Started's, Files' and Routines'; a change under `system/apps/screen/frontend/` rebuilds like any other frontend's.

- Before the restart it removes every registry row (`data/.state/apps.toml`) whose supervisord program the merged tree no longer declares, through `forward_port.py --remove`, so the old browser app's row stops offering a dead "Browser" tile in the launcher and a dead origin to the share gateway. A row with no program is never touched, and a program the tree still runs registers itself again on its next start.

- `migrate-workspace`'s legacy-layout reference sends `libs/browser/` to `system/apps/screen/`, as its path map already did.

- The apply no longer races the boot-time env-converge for the dpkg lock: its post-success `env-converge upgrade` step first waits (bounded, 15 minutes) for the one-shot env-converge program to finish -- the apply's own services restart re-runs it, and a unit with new pins (the Screen's stack) can keep it installing for minutes. Found by the upgrade end-to-end test, where the apply's `apt_full_upgrade` step failed with "Could not get lock /var/lib/dpkg/lock-frontend" while the Screen stack installed.
