- New `system/scripts/minds_browser_shim`, the `$BROWSER` a provider sign-in CLI runs in the workspace: it records the page to open for the chat app to hand to the minds desktop app.

- The gcp and azure templates no longer forward `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL` from the host; only `GH_TOKEN` is passed through.

- `migrate_claude_auth.py` still scrubs a `CLAUDE_CODE_OAUTH_TOKEN` from the host env file but no longer moves it into an account.
