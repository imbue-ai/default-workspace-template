New app, AI Usage Limits (`system/apps/ai_usage`): shows how much of each AI's usage limits the chats here have used and when each limit resets, for Claude, ChatGPT (Codex), Antigravity and OpenCode Go.

- Usage is read once per (kind of AI, account) from the endpoint that AI's own client uses, with the sign-in the chats already hold; every raw reading is kept in `data/.apps/ai-usage/readings.jsonl` and shown on the page.
- Each increase between two readings is credited to the chats that were working in that stretch (split evenly), from the activity the chats' hooks report and from Studio's chat list; the rest is shown as used elsewhere.
- One graph and one "What used it" log per limit, for all chats together or one chat; each starts over when its limit resets.
- Each stretch of work is labelled with a short AI-written summary of the message that started it, with the verbatim message one click away.
- It is a standalone uv project (own `pyproject.toml`, `uv.lock` and venv), the shape `build-app` scaffolds, on port 8080.
