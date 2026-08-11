Point `stop_hook.base_branch` back at `main` in `.reviewer/settings.json`, and state `verify_architecture.is_enabled` explicitly.

`base_branch` had been aimed at a feature branch as a safety net: with `fetch_and_merge` on, the stop hook merged `origin/main` into the workspace's `main`-named branch and pushed it back, leaking feature commits onto the template's main. `fetch_and_merge` is off at the repo level and the whole hook is off for workspace agents (`enabled_when: false`), so the leak needs both switches flipped -- while a base branch that doesn't exist in a workspace's own remote silently made every diff look empty.

`verify_architecture` was the one gate absent from this file, so it fell through to the plugin's default of enabled -- the opposite of what the rest of the file says. Recording it as `false` makes the config say what it means: for a workspace agent, every gate is off.

Both keys are overridable per checkout via the gitignored `.reviewer/settings.local.json`, which is how a template checkout nested in a mngr checkout for active development turns the gates back on.
