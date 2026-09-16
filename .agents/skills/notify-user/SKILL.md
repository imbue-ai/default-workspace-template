---
name: notify-user
description: "Tell the user, through the Mind app's notification feed (bell, badge, toast, and a system banner when they are away), that a very long-running task in THIS CHAT has finished. For chat agents only -- never for launch-task workers, whose results reach the user through their parent chat. Use after a task that ran roughly ten minutes or more (a multi-step build, a migration, a long test run), at your own judgment, never after every turn."
compatibility: Requires the latchkey gateway env mngr injects into every agent (LATCHKEY_GATEWAY, LATCHKEY_GATEWAY_PASSWORD); python3 only.
metadata:
  author: imbue
---

# Notify the user

The Mind app keeps a notification feed: a bell in the titlebar, a badge on the
dock icon, a toast card in every open window, and a system banner when the
user is looking at something else. This skill posts a message from this chat
into that feed. Clicking the notification lands the user in this chat.

## When to use it

Use it at your own judgment, for tasks that took long enough that the user
plausibly walked away: roughly ten minutes or more. Examples: a multi-step
build, a data migration, a long test run, a large download or export. One
notification per finished task, once, when it is done (or when it failed and
needs them).

Do NOT use it:

- after ordinary turns, or for anything the user is waiting on in this chat
  right now -- the reply itself is the notification;
- from a launch-task worker or any other sub-agent: a worker's results reach
  the user through the chat that launched it, and only chats show up in the
  app's feed;
- as a progress ticker. Never more than one per task.

## How

One sentence summarizing what was done, in the user's terms (what they can
now see, use, or decide), never the tool names or steps:

```bash
python3 .agents/skills/notify-user/scripts/notify_user.py "The migration finished: 3 tables moved and verified."
```

An optional `--title` becomes a prefix on the message in the feed and the
banner (`Title: message`):

```bash
python3 .agents/skills/notify-user/scripts/notify_user.py --title "Test run" "All 412 tests passed."
```

The script exits 0 when the app accepted the notification and non-zero when
it did not (the app was unreachable, or the gateway refused). **Read the exit
code.** When it fails, say so in your reply -- "I tried to notify you but the
notification did not go out" -- so the user knows why they heard nothing;
never retry in a loop.

## What happens on the other side

The app files the message under this chat's name and its workspace. In the
feed it reads as "<workspace> -- <this chat>" with your sentence beneath; the
system banner carries the same three lines. It stays in the feed until the
user clicks it, clears it, or opens this workspace; it never asks them to do
anything. Do not expect a reply through it: it is one-way.
