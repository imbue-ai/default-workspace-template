Workspace Claude auth moved off mngr host env vars and into the env block of the shared `CLAUDE_CONFIG_DIR/settings.json`, with the in-UI sign-in modal as the sole auth surface.

- The create templates no longer forward `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` via `pass_host_env`; credentials are written only by the system_interface backend into the settings env block (fully controlled: switching modes deletes the other mode's keys, so a stale credential can never shadow the new one).

- The sign-in modal's subscription path now drives `claude setup-token` (a 1-year token stored as `CLAUDE_CODE_OAUTH_TOKEN` in the settings env) instead of `claude auth login`; the modal shows the OAuth URL and polls until the browser approval completes, with a paste-code fallback and a subtle "already have a token" paste affordance. The Anthropic Console OAuth path was removed.

- New "Sign in with Imbue" path: a link to the Minds desktop app's key-mint page (keyed by this workspace's host id; remote access pops an alert to use the desktop app) plus a textarea for the copied `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` env-style blob. All paste paths share one strict endpoint that rejects unmanaged keys and mixed-mode pastes.

- Auth-change restarts now cover every claude-binary agent (`claude` AND `worker` types; previously workers were silently missed), snapshot agent states first, and send previously-RUNNING agents a "credentials updated, please continue" message after the restart so unattended work resumes. The `main` services agent is never touched.

- A persistent "Agent auth" entry below the chat (next to "Open agent terminal") opens the modal any time, with a muted header showing how the workspace is currently signed in. A page-load status check pops the modal on a freshly created (never signed-in) workspace, making sign-in the designed first-boot step.

- LiteLLM budget/auth rejection patterns were added to the transcript auth-error detection, so an exhausted daily budget also surfaces the modal.

- The `use-ai-integration` skill's keyed path now resolves credentials from the shared settings at call time (`read_workspace_ai_credentials()` in `claude_p.py`) instead of `os.environ`, so services pick up auth changes without restarts.

- **MIGRATION (existing workspaces):** run `uv run python scripts/migrate_claude_auth.py` from the repo root (from the workspace terminal or an agent -- the restart phase runs detached, so an agent invoking it on itself still completes). It moves any host-env Claude credentials into the settings env block, scrubs them from `$MNGR_HOST_DIR/env`, and restarts claude agents. Subscription-based workspaces need no migration.
