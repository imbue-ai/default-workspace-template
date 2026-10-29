The `use-ai-integration` helper (`claude_p.py`) now finds the saved Anthropic key file, `data/.secrets/anthropic.env`, from the workspace root instead of the current folder.
Before, running the helper from another folder (for example its own `scripts/` folder) wrote the key under that folder, outside the ignored root `data/`, and a service started from another folder did not find the saved key.
The helper walks up from its own location to the folder holding `system/scripts`, so a copy inside an app finds the same file. Outside a workspace it still uses the current folder.
Inside a git worktree (where background workers run), the helper uses the main checkout's `data/`, so a key saved by a worker is the one services in the main checkout read.
