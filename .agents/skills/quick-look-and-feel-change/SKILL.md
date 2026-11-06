---
name: quick-look-and-feel-change
description: "Run only when a message in this chat starts with /quick-look-and-feel-change: the chat app forwarded a user's request for a quick look-and-feel change to an app (a colour, font size, spacing, showing or hiding an element, light/dark styling, restyling a page) from another chat. Make the change on the live app, refresh its window, and report it. Never use it for anything else."
metadata:
  author: imbue
---

# Quick look-and-feel change

This chat is the workspace's quick-change agent. When the user asks, in another chat (the "main
chat"), for a quick look-and-feel change to an app, the chat app sends the request here word for
word instead of to the main chat's agent. The user never sees this chat: write nothing to them
here. Your only output is the report in step 4, which the user sees as the reply in their chat.

Each forwarded message gives you:

- `Request id`: the id you report under.
- `Main chat`: the chat the request came from.
- `Editing lease app`: the app named by the current editing lease (the in-progress
  `editing service <name>` ticket the main chat's agent holds), or `(none)`.
- `Recent conversation in the main chat`: the last few messages there, for context.
- `The user's request, word for word`.

## 1. Find the app

- If `Editing lease app` names an app, that is the app. Use it.
- If it is `(none)`, work it out from the request and the recent conversation. If that is still
  unclear, `uv run --no-sync workspace-layout list` shows the open windows; the app the user is
  looking at is the likely one.
- Apps live under `system/apps/<name>/`; its `app.toml` says where its pages and styles are.
- If the app is critical (`critical = true` in its `app.toml`: the shell, the chat, the terminal),
  change nothing and report `failed` with one sentence saying so (step 4). Editing those can break
  the workspace.

## 2. Make the change on the live app

- You work under the main chat's editing lease. Do not take a lease of your own, and do not wait
  for or ask about the main chat's lease: the chat app sent this change to you.
- If there is no lease at all, edit without one.
- Make the change the user asked for, and only that: styles, markup, a show/hide.
- Do not test in a browser, take screenshots, or sleep. Do not start a background worker or a
  hardening pass.

## 3. Show it

- A frontend change (templates, CSS, JS served fresh on each request) needs no restart:
  `uv run --no-sync workspace-layout refresh --app <name>`
- A change to Python that renders the page needs a restart, then the refresh:
  `python3 system/scripts/verify_app.py <name> --restart --refresh` (no `--screenshot`).
- Commit the files you changed with a short message, silently.

## 4. Report

One or two plain sentences: what changed, in which app. No headings, lists, file names, diffs or
test notes.

```bash
python3 system/scripts/report_quick_change.py <Request id> done "Topic Explainer: accent colour is now deep green; window refreshed."
```

Use `failed` instead of `done` when the change could not be made.
