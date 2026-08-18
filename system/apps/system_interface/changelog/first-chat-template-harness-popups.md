Scoped fast mode to the workspace's first chat via a `first` create template, replaced the workspace-wide fast-mode decision with a per-agent ask-once prompt, and moved the composer's slash-command guards and the agent-auth surface onto per-harness declarations (`HarnessSpec.popups` / `auth_modal`).

This entry describes the end state relative to main.

## The `first` create template

`[create_templates.first]` (`.mngr/settings.toml`) owns everything unique to the workspace's opening chat: the `/welcome` message, the `first=true` label, and the fast-mode launch settings for both fast-capable harnesses (`agent_types.claude.settings_overrides.fastMode=true`; codex's `config_overrides__extend` gains `service_tier = "fast"` + `features.fast_mode = true`, which land verbatim in its per-agent config.toml). The bootstrap creates the initial chat with `--type claude -t first -t chat` and no longer reads a fast-mode decision file or passes `--message`.

No other chat launches fast: the UI create path passes no launch settings at all (`HarnessSpec.launch_settings_overrides` is gone), and `[agent_types.claude]`'s `fastMode = false` default is what every non-first chat gets. The `data/.state/fast_mode_decision.json` file, its `GET/POST /api/workspace/fast-mode` endpoints, and `harnesses/claude/launch_defaults.py` are deleted; stale decision files in existing workspaces are simply ignored.

Behavior change for existing workspaces: a recorded "keep fast mode" answer no longer makes new chats launch fast.

## The ask-once fast-mode prompt

The grace-period prompt (5 user turns) is now agent-scoped and harness-declared. It fires only for an agent whose harness declared the `fast_mode_prompt` turn check, that carries the `first=true` label, whose model state still reads fast, and that has not been asked before. Any exit from the modal latches the answer via the agent label `fast_mode_prompt_answered=true` (new `POST /api/agents/<id>/fast-mode-answered`, which shells `mngr label`); "Switch to standard speed" additionally sends the fast-off switch through the agent's own harness resolver. The modal names the agent it is asking about. Labels reach the frontend with the observe relist, so an in-session answered set suppresses the prompt while the label write propagates.

## Harness-declared popups

`HarnessSpec.popups` declares the chat UI's popups per harness, shipped to the frontend inside the `/api/harnesses` payload and acted on by lookup — the frontend no longer contains any `harness === "claude"` behavior branch. A popup is `{trigger, commands, action}`: `composer_command` popups match a typed message's first token at send time; the `turn_check` popup is the fast-mode check above.

- claude declares `open_auth` for `/login` `/logout`, its measured declined-command list (moved out of the frontend's `claudeSlashCommands.ts`), and the fast-mode check.
- codex declares `open_auth` for `/login` `/logout`, declines `/archive /btw /clear /delete /effort /exit /experimental /fast /fork /keymap /model /new /plan /quit /resume /side /vim` (its messages go over the app-server, so slash commands would reach the model as prose — and `/model` `/fast` `/effort` would additionally be hidden from the transcript by the shared display rules), and the fast-mode check.
- pi declares `open_auth` for `/login`, and declines `/clone /compact /fork /llama /model /name /new /quit /resume /scoped-models /session /tree` (pi is driven through its lifecycle extension's inbox rather than by typing into the pane, so these would reach the model as prose; the session-switching ones would also strand the chat's view of the conversation).

The composer guard matches on the first token for auth commands too, so `/login please` now intercepts (it used to slip past the whole-message match). A slash-shaped message awaits the catalog load once when it has not landed yet, so an early `/login` cannot slip through the fetch window; the catalog fetch is now single-flight and retries after a failure.

## The per-harness agent-auth surface

`HarnessSpec.auth_modal` declares what "agent auth" opens: `managed` (claude) is the existing in-app login modal, unchanged; `terminal` (codex, pi) is a new shared notice rendering the harness's `auth_instructions` ("open the agent's terminal and run /logout then /login", etc). Every auth entry point routes through one dispatch — the composer's auth intercept, the chat footer's "Agent auth" entry, and the stream/snapshot auth-error hooks.

## First-chat launchers

Behind `FEATURE_FLAG_ENABLE_OTHER_HARNESSES`, the new-tab menu gains "New first Claude chat" and "New first Codex chat", which create a chat with the `first` template stacked (`CreateChatRequest.first`), so the first-chat flow — fast launch, `/welcome`, the grace-period prompt — can be exercised without re-creating a workspace.
