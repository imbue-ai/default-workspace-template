# Changelog - workspace template

A concise, human-friendly summary of changes to the workspace template, the repository every Imbue Studio workspace is created from and updated to. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [minds-v0.10.5] - 2026-11-17

This release carries two fixes. `build-app` no longer gives a new app port 8091, the Screen's video server port, and an update moves an existing app off 8090 or 8091. Installing frontend dependencies on Linux x64 no longer fails when onnxruntime-node's CUDA download answers 404.

### Fixed

- Fixed: `build-app` no longer gives a new app port 8091, the Screen's video server port, which broke the Screen and the agent browser; an update moves an existing app off 8090 or 8091.
- Fixed: Installing frontend dependencies (image builds, update-self) no longer fails on Linux x64 when onnxruntime-node's CUDA download answers 404; the new `system/.npmrc` skips that download.

## [minds-v0.10.4] - 2026-11-13

The taskbar tray gains a resource gauge that warns when disk, memory or CPU runs short, and update-self restarts idle chat and background agents when an update changes what their harness loaded at start. Tray speech bubbles now show only a chat's notifications and messages that mention the viewer, and the harnesses move to newer versions, with Fable 5.2 and its fast mode in the model bar. Fixes cover Codex chats losing their fast-mode speed, messages with pasted binary content failing to send, the memory guard stopping apps during large backups under runc, and `migrate-workspace`; paramiko moves to 5.0.1 for a security fix.

### Added

- Added: A resource gauge in the taskbar tray warns when disk, memory or CPU runs short, sends an Imbue Studio notification that opens System Monitor on that resource, and tells claude and codex agents through the `system/scripts/agent_resource_notice.sh` prompt hook. Its bars and warning levels are set from "Resource gauge..." on the desktop menu.
- Added: update-self restarts the workspace's idle chat and background agents when an update changes what their harness loaded at start (`update_self.py agent-restart-verdict`, `restart-agents`, `restart-self`), and names the busy ones it left running.
- Added: The chat app's interrupt route (`POST /api/chats/<id>/interrupt`) takes `only_if_idle` and `keep_queue`, and `message_chat.py --interrupt` restarts a chat's agent through it.

### Changed

- Changed: Tray speech bubbles show only a chat's notification to the user and messages that mention the viewer. The notify-user script also posts each notification as a bubble, the agent's turns in its own chat are no longer posted to the tray, and `BubblePost` and the `bubble` push carry a `kind` (`message` or `notification`).
- Changed: Clicking the agent's avatar in the tray shows the pinned chat window on the chat it holds instead of switching it to the main chat.
- Changed: Harness binaries move to Claude Code 2.1.323, Codex 0.169.0, OpenCode 1.19.5 and the Antigravity CLI 1.5.8. The model bar's Fable entry is now Fable 5.2, with fast mode.
- Changed: mngr is pinned to its `minds-v0.10.4` release, the one the 0.10.4 desktop app ships.
- Changed: The `datalib` skill installs datalib v0.54.1: each message of a document renders on its own with its sender's time, and plain-text email shows as typed.
- Changed: Chat agents act on hook feedback without mentioning it to the user.

### Fixed

- Fixed: Codex chats keep the speed their fast-mode setting asks for; the agent terminal no longer undoes it when it attaches or after a restart.
- Fixed: A message holding pasted binary content no longer fails to send: the composer drops NUL characters.
- Fixed: Under runc the memory guard counts unmapped file cache and reclaimable slab as headroom, so a backup of a large tree no longer gets the workspace's apps and the backup itself stopped every hour.
- Fixed: The `migrate-workspace` skill no longer copies the old workspace's share identity, and recreates chats with their harness, account and work folder.

### Security

- Security: paramiko moves to 5.0.1 (CVE-2026-44405) through a root-project override.

## [minds-v0.10.3] - 2026-11-09

In shared workspaces, a visitor is no longer put on someone else's desktop or in viewing mode on their own, "Stop viewing" says why when it cannot act, and the roster and presence heartbeat answer within the chat app's time limit. In chat, a message sent during a provider switch no longer briefly disappears, the status row no longer jumps above the message that started the turn, and sender names no longer stay as they were when the page loaded. An app's crystallize pass now also checks the app on a phone and reviews what visitors can reach when it is shared, and the `imbue-studio` skill creates a new workspace where and as large as the agent's own unless asked otherwise.

### Added

- Added: Chat pages report when they are actually on screen (`minds:chat-on-screen`, embed contract v15; the shell announces `reportsChatOnScreen`), so Imbue Studio keeps its wash over a new workspace until the welcome chat appears.
- Added: An app's crystallize pass checks the app on a phone (touch, no sideways scroll, a committed phone-sized browser test) and reviews what visitors can reach when it is shared (request guard, owner-only secret routes, safe rendering of visitor content, an access log when visitors can change things); the worker's `done` report gains `Phone:` and `Sharing:` lines.

### Changed

- Changed: Pinned mngr (`pyproject.toml`, `uv.lock`) to the `minds-v0.10.3` release, the one the 0.10.3 desktop app ships.
- Changed: The `imbue-studio` skill creates a new workspace where and as large as the agent's own (`launch_mode`, `cloud_account`, `instance_type`, `disk_gb`) unless the user asks otherwise, rather than at the API's defaults; it finds the agent's own workspace by its primary agent's id, and lists connections with Studio's new `GET /api/v1/workspaces/cloud-accounts`.
- Changed: A person with no name, email or arrival is shown as "Guest" (`workspace_layout.roster.UNNAMED_PERSON_DISPLAY_NAME`) in the roster and chat instead of their raw account id.

### Fixed

- Fixed: A visitor's desktop made before minds-v0.10.0 reading as the workspace owner's, which put the visitor in viewing mode on their own desktop; on its first start after the update the shell gives each such desktop back to its visitor.
- Fixed: A visitor whose recorded desktop id now names someone else's desktop landing on that desktop; they get a fresh one on arrival.
- Fixed: "Stop viewing" silently doing nothing when the workspace did not answer or the viewer had no desktop of their own to go to; it now shows a toast saying why.
- Fixed: `GET /api/roster` and the presence heartbeat exceeding the chat app's two-second limit while re-fetching profiles; they now answer from the profile cache and refresh it in the background.
- Fixed: A message sent during a chat's provider switch briefly disappearing from the page while the switch delivered it.
- Fixed: The status row ("Thinking…", a running tool) jumping above the message that started the turn before that message reached the conversation.
- Fixed: Chat sender names staying as they were when the page loaded; an unfamiliar author name now re-reads the roster.
- Fixed: An update's pre-flight check leaving the shell's state and presence files in the workspace's source tree, so the next update refused to run over uncommitted changes; the check now keeps them in a scratch directory.

## [minds-v0.10.2] - 2026-11-09

This release pins mngr to its minds-v0.10.2 release and fixes two Screen problems. x64 Screen image builds and first-boot installs no longer fail on a checksum mismatch after the Fortress download was re-uploaded upstream, and the Screen no longer re-grants control to a session that has just returned it, which dropped the next person's first input.

### Changed

- Changed: Pinned mngr (`pyproject.toml`, `uv.lock`) to the `minds-v0.10.2` release, the one the 0.10.2 desktop app ships.

### Fixed

- Fixed: x64 Screen image builds and first-boot installs failing with a SHA256 mismatch after upstream re-uploaded `fortress-v156-linux-x64.tar.gz`; the pin now follows the new asset (same Chromium binary).
- Fixed: The Screen re-granting control to a session that had just returned it, which dropped the next person's first input; the service now ignores neko's echoes of its own host changes, and take and return wait for the lease to reach neko before answering.

## [minds-v0.10.1] - 2026-11-09

The chat gains on-device dictation and a new layout for the agent's side, with every reply in a bubble and tool calls on a timeline between them, and backups now respect the account's Imbue Cloud storage quota. Built-in code calls the workspace's own Python in-process instead of spawning it, a test asks for a stated reason for every launch of the workspace's own code, and new dependency versions are picked up only once they are two weeks old. Fixes cover a container that docker restarted on its own coming back with only sshd running, copy and paste with the Screen, desktop CPU use and message sending. The shell and built-in apps now refuse requests a browser marks as coming from another origin's page, so a page shown in the workspace's own browser can no longer act as the owner.

### Added

- Added: Dictation in the chat's composer: a mic button (Cmd/Ctrl+Shift+M) turns speech into text on the user's own device, with a speech model the browser downloads and keeps (new `system/libs/workspace_voice` library; an app's `uses_microphone` manifest key hands the microphone to its frames).
- Added: `run-on-users-computer` skill: only on an explicit request to run something on the user's own computer, the agent asks through Imbue Studio's remote-execution approval (a "This computer" permission card); everything else runs in the workspace.
- Added: Backups respect the account's Imbue Cloud storage quota: over quota a tick prunes to free space instead of backing up, a store refusal for lack of space is reported as `quota_exceeded`, and the System Monitor shows "Out of backup storage".
- Added: A notification when a chat cannot reply because its AI account is signed out; clicking it opens the chat.
- Added: An app can declare several default desktop shortcuts (`[[default_shortcut]]`), and the Screen offers both Screen and Browser. `AppManifest`, the registry entry and `GET /api/inventory` carry a `default_shortcuts` list in place of `default_shortcut`.
- Added: `build-app` shows a new app inside a "UI Sketch" frame noting what is not real yet, until the app reads its real data (`mock_frame.py up|refresh|down`).
- Added: `disk_headroom` library and `uv run --no-sync disk-headroom` command: the space the workspace can still write, including a cloud machine's disk quota. The System Monitor's disk view, the caretaker's disk check and `serve_isolated_instance.py --copy` use it.
- Added: A guard on every harness blocks a command that waits on a foreground `sleep` of 5 seconds or more, pointing the agent at a background command or a short poll loop.
- Added: `run_in_background.py --waiter` for a wait that is safe to restart: it runs in a low-shed OOM band and is restarted if shed while its agent still runs. The built-in skills' waits use it.
- Added: Usage signals for the explorer analytics collection: the shell records `window_opened` and `window_input` client-activity events, and the share gateway writes per-minute request counts. The gateway also keeps a header-stripped per-request access log, which is never collected.

### Changed

- Changed: The workspace's mngr is pinned to the `minds-v0.10.1` release.
- Changed: The agent's side of the chat follows the multiplayer-chat layout: its name and face once per stretch of output, every reply in a bubble, tool calls and steps on a timeline between the bubbles, and bash-mode commands on the user's side.
- Changed: A reply the agent is still writing streams in under the step it belongs to rather than below the chat.
- Changed: Chats and the avatar count an agent whose turn has ended while its background work still runs as working, not stopped or idle (chats created after the update).
- Changed: The chat's avatar floats over the windows by default on a screen with no saved placement; the update moves every screen's saved avatar placement into the taskbar once (`clients.json` version 3), and a later choice sticks.
- Changed: Send-failure notices say what happened in the chat's own words, Force is now Restart, and "Sent again" comes from the chat app, so it shows in every window and after a reload.
- Changed: Attachments show under the message without a "See attachment here:" label, and a just-sent image shows at once, in a placeholder sized to the image while it loads.
- Changed: `connect-external-service` reaches an MCP server through latchkey (an OAuth sign-in through latchkey's `mcp` login flow, or a custom service with a key) and no longer runs a published MCP server package in the workspace.
- Changed: The `datalib` skill installs datalib v0.52.0: read state lives only in a store's `read_marks` table, the `fsindex` source type is now `file_inventory`, and search's `tab=` and `--tab` are gone.
- Changed: Latchkey is bumped to 3.22.1.
- Changed: The main chat is never paused or stopped to free memory, since the chat app restarts it at once: `run-status stop-chats --agent` refuses it and `memory_candidates.py` leaves it out.
- Changed: `system/test_python_launch_rules.py` fails every launch of the workspace's own code and every import-path edit unless a `# own-process:` or `# import-path:` marker gives a reason, and an update restructures user code that fails it.
- Changed: Built-in code calls the workspace's own Python in-process instead of spawning it. The app registry writer moved into the new stdlib-only `system/libs/app_registration` library, so `app-manifest` depends on it and an update relocks user packages; host_backup's `env_record_capture_completed` event carries `error_message` in place of `exit_code` and `stderr`.
- Changed: Type-check ratchets run `ty` 0.0.87, and the routines library's `Duration.count` is renamed `Duration.amount`.
- Changed: The root project, scaffolded apps and migrated user packages carry a two-week dependency cooldown (`[tool.uv] exclude-newer = "14 days"`, requiring uv 0.10 or later).
- Changed: The System Monitor's disk breakdown counts `/var/cache/user` under Package caches and adds a "Chats, logs and settings" row for the home folder.
- Changed: A theme's `--desk-window-radius` must be a `px` length from 0 to 32px.

### Removed

- Removed: `mcpc` is no longer installed or used; an existing workspace keeps its install and the connections made with it.

### Fixed

- Fixed: A workspace container that docker restarted on its own (an out-of-memory kill, a daemon restart, a VM reboot) relaunches its services through a container start hook instead of coming back with only sshd running.
- Fixed: Copy and paste between the user's computer and the Screen works over a shared link, text copied from a terminal or editor pastes, and clicking into the Screen no longer prompts for clipboard access.
- Fixed: A workspace updated off the old browser app no longer keeps a broken Browser shortcut: shortcuts of an app that is no longer registered are removed from every desktop.
- Fixed: The roster lists people a workspace-level email-domain grant admitted ("Here through <domain>"), and names a granted account that has not arrived yet instead of showing its id.
- Fixed: A busy desktop no longer holds the browser above 100% CPU or flickers: hidden and fully covered windows stop rendering, and minimized windows keep their size and scroll position.
- Fixed: A message whose send got no answer is no longer reported as failed or delivered twice, and a message refused by an expired sign-in is sent again once the account signs back in.
- Fixed: In a shared workspace, a queued or held message shows under its sender instead of as the viewer's own with a raw `[from: <name>]` prefix.
- Fixed: A shared workspace with many open chats no longer stalls the chat service with memory-priority passes.
- Fixed: Scrolling to the bottom of a chat while the agent writes stays there, and a growing message box no longer makes the transcript jump.
- Fixed: A Claude agent's streamed reply no longer repeats earlier text with a "Ran N shell commands" summary attached.
- Fixed: ChatGPT sign-in no longer asks the user to turn on OpenAI's removed "device code sign-in" setting.
- Fixed: An app that stops when no window shows it gets its full 60 seconds again after each restart, so `build-app`'s restart no longer stops it while its page loads.
- Fixed: The launch-rules test no longer flags a workspace with exactly one crystallized skill, and a migrated app's type check no longer fails on the built-in skills' imports.

### Security

- Security: The shell and the built-in apps (datalib's server already requires its own token) refuse a request a browser marks as coming from another origin's page, and treat a request with no identity header as the owner's only from loopback (`app_schema.request_guard`); the Screen's Chromium also blocks pages' requests to loopback. A page shown in the workspace's own browser can no longer act as the owner.
- Security: `use-ai-integration` always writes its Anthropic key snapshot to `data/.secrets/anthropic.env`; run from another directory, it could land in a git-visible file whose diff, key included, Claude Code records in the session transcript.

## [minds-v0.10.0] - 2026-11-02

Workspaces become multiplayer: a roster and a taskbar tray show the people and the agent, chats show who said what, people talk in chat rooms and mention each other, and each desktop belongs to a person. The new Screen app streams a Linux desktop into a workspace window and replaces the browser app. Python packaging is reworked: skills and scripts are uv projects, built-in apps share one venv while user-built apps, services and libs get their own, and Python 3.12 is the floor, so local workspaces from before 2026-09-14 need `migrate-workspace` instead of an update. New Lima VMs boot an image with a kernel CVE fix, and the gcp and azure create templates no longer forward `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` and `GH_TOKEN` into the workspace.

### Added

- Added: The Screen app: a streamed Linux desktop (neko, XFCE) in one workspace window, with a control lease people and agents share, sizing to the controller's window, phone touch input, clipboard and file drop, the `screen` command and `use-screen` skill, and stream relaying through the share gateway for visitors.
- Added: Multiplayer presence: the shell's roster (`GET /api/roster`) and a taskbar tray of the people and the agent, with hover cards, activity status and speech bubbles for new messages.
- Added: Chats know who said what: a message from anyone but the owner reaches the agent as `[from: <name>] <text>` and is drawn under the sender's name and picture.
- Added: Chat rooms (chats among people with no agent, the new `room` agent type), `@` mentions of people, the agent and other chats, and a "Lounge" room that clicking a person drafts a mention into.
- Added: Desktops belong to people: a desktop `owner`, a "Manage Desktops" overview in place of the desktop switcher, and read-only viewing of another person's desktop from their card.
- Added: A "Chat about this app" balloon in every window's title bar opens the app's own chat beside the window; the standard title bar now centres the icon and title.
- Added: Rebindable keyboard shortcuts, saved for the account or for one workspace, from the backdrop menu's "Keyboard shortcuts..." dialog; apps declare their actions under `[[keybindings]]` in `app.toml`.
- Added: A `FULLSCREEN` window state (`workspace-layout place --state fullscreen`) and Imbue Studio's immersive mode.

### Changed

- Changed: Python packaging overhaul: `system/scripts` and each skill's Python (`.agents/skills/<name>/python/`) are uv projects run from their module paths, one-offs use `uv run --no-sync`, and programs exec from `.venv/bin` instead of under `uv run`; an update migrates a workspace's own skills per `update-self/references/python-packaging-migration.md`.
- Changed: Built-in apps run from the one workspace venv instead of per-app uv tools, and user-built apps, services and libs are standalone uv projects with their own lock and venv (`build-app` scaffolds them so; an update converts existing ones).
- Changed: Every `.venv` is a symlink to a directory beside the uv cache rather than a ~512 MB copy on the workspace volume, and update-self's apply syncs a fresh venv and switches the link at restart.
- Changed: Python 3.12 is the workspace floor; local workspaces from before 2026-09-14 (system Python 3.11) can no longer update in place and need `migrate-workspace`.
- Changed: Fortress upgraded from v151 to v156; it replays human cursor motion, so a click that moves the pointer takes up to about a second.
- Changed: The chat resends a message once when mngr cannot confirm it arrived (marked "Sent again"), and retries an agent that is not ready or unreachable before showing a failure.
- Changed: The chat app answers as soon as it starts and shares one file watcher across chats, cutting the threads each viewed chat cost.
- Changed: Idle compaction shows its `/compact` on the "Compacting while idle" chip rather than as a user message, and it no longer counts as a turn.
- Changed: The workspace timezone comes from `data/.state/user_timezone`, written once at create, instead of a boot-time fetch from the desktop, and the backup-verification-off flag lives in the workspace (`verification_disabled` in `data/system/backup.toml`).

### Removed

- Removed: The browser app; the browser is now a launch path of the Screen (`workspace-layout open https://...`), `agentic-browser-fleet` keeps its verbs, and an update keeps existing browser profiles and tabs.

### Fixed

- Fixed: A message sent to a Claude chat while it is starting is no longer reported as possibly unsent, or lost when the agent's process exits before recording it.
- Fixed: Switching between chats no longer leaves the transcript blank and scrolled to the top.
- Fixed: The chat file watcher no longer leaks a thread and drops a watch when the OS refuses to start a thread.
- Fixed: Bug reports from large workspaces no longer lose their whole archive by overrunning the collection budget.
- Fixed: `routines migrate-cron` converts cron lines with shell syntax into working routines, and carries over the state of jobs that never ran on minds-v0.8.7 (still under `system/data/.state/jobs/`).

### Security

- Security: New Lima VMs boot the Debian 13 `20261026-2643` image, which carries the CVE-2026-98432 kernel fix.
- Security: The gcp and azure create templates no longer forward `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` and `GH_TOKEN` into the workspace's host env.

## [minds-v0.9.5] - 2026-10-30

A chat agent can now launch a coding agent in the user's own repository or directory with the new `launch-custom-agent` skill, Codex, Pi and Antigravity replies show while they are being written, and a Style Guide app catalogues the workspace design system. Fixes stop the OOM prioritizer from shedding an unrelated process, such as the chat app, that reuses an agent's process id, stop the Claude shoulder tap from dropping a queued message, and stop taps on a phone's text fields from zooming the shell. The share gateway's access log no longer records query strings, so visitors' sign-in tokens no longer land in it.

### Added

- Added: `launch-custom-agent` skill and `custom` create template, so a chat agent can launch a coding agent in the user's own repo or directory that runs without the workspace's chat instructions yet appears as a chat in Imbue Studio.
- Added: Codex, Pi and Antigravity replies show in the chat while they are being written, as Claude replies already do.
- Added: Style Guide app (`system/apps/style_guide`), one page cataloguing the workspace design system's tokens, icons and components in the active theme; it does not start at boot (`supervisorctl start style-guide`).

### Changed

- Changed: Pinned mngr (`pyproject.toml`, `uv.lock`) to the Imbue Studio 0.9.5 release, the one the 0.9.5 desktop app ships.
- Changed: The `datalib` skill installs datalib v0.48.0 (from v0.46.0), which fixes Slack syncs failing on truncated replies, broadens Notion sources and search, and lets an agent propose contacts for the user to confirm in the contacts card's new Review tab.
- Changed: Bug reports withhold only the files the secret scan flags rather than their whole group, and blank credentials in URL query strings before scanning.
- Changed: `build-app` serves the look-and-feel mock from the scaffolded app and shows pictures of the design in chat only when the user asks for them.
- Changed: The chat list shows no mark for idle or stopped chats; a stopped chat's row stays dimmed.
- Changed: On a phone, the bottom bar steps aside while the soft keyboard is up.
- Changed: `memory_candidates.py` reports anonymous memory (`anonymous_kib`) instead of summed `VmRSS`, which read 2-10x high under gVisor, and adds `--free-memory`.

### Fixed

- Fixed: Chat creates, renames and routines no longer fail during an update while the new `.mngr/settings.toml` is live but mngr is not yet reinstalled: supervised programs run with `MNGR_ALLOW_UNKNOWN_CONFIG=1`, which takes effect from the update after this one.
- Fixed: The OOM prioritizer and System Monitor count a registered pid as an agent's only while it is still the process that registered, so an unrelated process that reuses the pid, such as the chat app, can no longer be tagged and shed as an abandoned chat; every live process of a chat (e.g. codex's TUI and app-server) is now re-tagged.
- Fixed: The Claude shoulder tap no longer silently drops a queued message when its cancel lands on the turn that had already absorbed it; it sends its recovery nudge instead.
- Fixed: The provider chooser retries a failed provider-list load for about 15 seconds before showing "Could not load providers", so a brief connection drop no longer strands a new user on that screen.
- Fixed: Signing in to Google (Antigravity) no longer lets agy's background updater replace the workspace's pinned `agy`.
- Fixed: Tapping a text field on a phone no longer zooms the shell in (pinch-zoom no longer works in the home-screen app or Android Chrome).
- Fixed: The Routines pages scroll again.

### Security

- Security: The share gateway's access log no longer records query strings, so share sign-in tokens, which carry the visitor's email, no longer land in `share-gateway.log`.
