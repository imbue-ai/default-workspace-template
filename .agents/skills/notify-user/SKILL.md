---
name: notify-user
description: "Tell the user, through a notification in the Imbue Studio app, that a turn in THIS CHAT ended with something they will want to know about or act on: a finished deliverable, a result they were waiting for, or a question only they can answer. For chat agents only -- never for launch-task workers, whose results reach the user through their parent chat. Not for chitchat, acknowledgements, progress with no result yet, or trivial answers."
compatibility: Requires the latchkey gateway env mngr injects into every agent (LATCHKEY_GATEWAY, LATCHKEY_GATEWAY_PASSWORD); runs from the root venv (`uv run --no-sync`).
metadata:
  author: imbue
---

# Notify the user

This skill posts a message from this chat to the Imbue Studio app's
notification menu; clicking it brings the user here. Unless the user is already
looking at this chat, the same message also goes to every device the owner
turned notifications on for (a phone with the workspace on its home screen, a
browser), where tapping it opens this chat. It is one-way: any answer comes
back in this chat.

## When to use it

**When a turn ends with something the user will want to know about or act
on:** a finished deliverable, a result they were waiting for, or a question
only they can answer (including a failure that needs them). The amount of work
or time is not the test; what matters is whether the user cares about the
outcome. A quick result they were waiting for is worth one line; an hour of
groundwork with nothing to show yet is not. A question
you cannot go on without is a reason to send one, since the user may not be
looking. One notification per turn, sent once, as the turn ends.

Skip it when the turn produced nothing of that kind: chitchat, an
acknowledgement, progress with no result yet, or the answer to a trivial
question.

Do NOT use it:

- from a launch-task worker or any other sub-agent: a worker's results reach
  the user through the chat that launched it;
- as a progress ticker. It goes out as the turn ends, never partway
  through, and never more than once per turn;
- for a reply posted into a chat room (`mngr message --start <room>`): the reply in the
  room is the notification.

No hook reminds you at the end of the turn: the decision is yours as you finish, and
it is never narrated to the user.

## How

One sentence saying what is ready or what you need from them, in the user's
terms (what they can now see, use, or decide), never the tool names or steps:

```bash
python3 .agents/skills/notify-user/python/notify_user_skill/notify_user.py "The migration finished: 3 tables moved and verified."
```

An optional `--title` becomes a prefix on the message (`Title: message`):

```bash
python3 .agents/skills/notify-user/python/notify_user_skill/notify_user.py --title "Test run" "All 412 tests passed."
```

The script exits 0 when the app, or at least one of the user's devices,
accepted the notification and non-zero when neither did (the app was
unreachable, or the gateway refused, and no device took it). **Read the exit
code.** When it fails, say so in your reply -- "I tried to notify you but the
notification did not go out" -- so the user knows why they heard nothing;
never retry in a loop. A `could not check who is watching this chat` warning
is not a failure: the notification still went out, and the exit code says so.

The devices are optional: with none turned on the push is skipped silently,
and a device that did not take it is a warning on stderr, not a failure. To
turn notifications on for a phone, the user opens the launcher in the workspace
on that phone (from its home screen on an iPhone or iPad) and chooses
"Notifications on this device".
