---
name: quick-look-and-feel-change
description: "Run only when a message in this chat starts with /quick-look-and-feel-change: the chat app forwarded a user's request for a quick look-and-feel change to an app (a colour, font size, spacing, showing or hiding an element, light/dark styling, restyling a page) from another chat. Make the change on the live app, refresh the user's window, and report to the main chat. Never use it for anything else."
metadata:
  author: imbue
---

# Quick look-and-feel change

This chat is the workspace's quick-change agent. When the user asks, in another chat (the "main
chat"), for a quick look-and-feel change to an app, the chat app forwards the request here word
for word. The main chat's agent has been told this chat is making the change and that it must not
make it itself. Your job is to make the change fast, show it, and report back.

Each forwarded message gives you:

- `Main chat`: the chat id to report to.
- `Recent conversation in the main chat`: the last few messages there, for context (which app the
  user means, what "it" refers to).
- `The user's request, word for word`.

## 1. Find the app

- Work out which app the request is about from the request and the recent conversation.
- If that is unclear, `uv run --no-sync workspace-layout list` shows the open windows; the app the
  user is looking at is the likely one.
- Apps live under `system/apps/<name>/`; read its `app.toml` for where its pages and styles are.
- If the request is not a look-and-feel change after all (it needs a new feature, new data or
  backend work), or the app is critical (`critical = true` in its `app.toml`: the shell, the chat,
  the terminal), change nothing and go straight to step 4 with a report that says so, so the main
  chat's agent takes it over.

## 2. Make the change on the live app

- Edit the app's files directly. Keep the change to what was asked: styles, markup, a show/hide.
- Do not test in a browser, take screenshots, or sleep. Do not start a background worker or a
  hardening pass.

## 3. Show it

- A frontend change (templates, CSS, JS served fresh on each request) needs no restart:
  `uv run --no-sync workspace-layout refresh --app <name>`
- A change to Python that renders the page needs a restart, then the refresh:
  `python3 system/scripts/verify_app.py <name> --restart --refresh` (no `--screenshot`).
- Refresh after each change, so the user sees each one land.
- Commit the change (`git add` the files you changed, then `git commit -m "<why>"`), silently.

## 4. Report to the main chat

Write a short report to a file and send it:

```bash
python3 system/scripts/message_chat.py <Main chat id> --message-file <file>
```

Wrap it like this. The summary line is what the user sees; the rest is for the main chat's agent:

```
<background-task-report><summary>Made the header dark blue in Todo and refreshed its window.</summary>
App: todo. Files changed: system/apps/todo/static/style.css (header background #1e3a8a).
</background-task-report>
```

If you changed nothing (step 1), say why in the summary, for example
`<summary>Not a quick style change: this needs a new feature, so the main chat should handle it.</summary>`.

Keep everything you write short and plain. Do not message the user here beyond one line saying
what you changed; the main chat tells them.
