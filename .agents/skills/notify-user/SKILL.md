---
name: notify-user
description: "Tell the user, through a notification in the Imbue Studio app, that a turn in THIS CHAT ended with something they will want to know about or act on: a finished deliverable, a result they were waiting for, or a question only they can answer. For chat agents only -- never for launch-task workers, whose results reach the user through their parent chat. Not for chitchat, acknowledgements, progress with no result yet, or trivial answers."
compatibility: Requires the latchkey gateway env mngr injects into every agent (LATCHKEY_GATEWAY, LATCHKEY_GATEWAY_PASSWORD); python3 only.
metadata:
  author: imbue
---

# Notify the user

This skill posts a message from this chat to the Imbue Studio app's
notification menu; clicking it brings the user here. It is one-way: any answer
comes back in this chat.

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
  through, and never more than once per turn.

No hook reminds you at the end of the turn: the decision is yours as you finish, and
it is never narrated to the user.

## How

One sentence saying what is ready or what you need from them, in the user's
terms (what they can now see, use, or decide), never the tool names or steps:

```bash
python3 .agents/skills/notify-user/scripts/notify_user.py "The migration finished: 3 tables moved and verified."
```

An optional `--title` becomes a prefix on the message (`Title: message`):

```bash
python3 .agents/skills/notify-user/scripts/notify_user.py --title "Test run" "All 412 tests passed."
```

The script exits 0 when the app accepted the notification and non-zero when
it did not (the app was unreachable, or the gateway refused). **Read the exit
code.** When it fails, say so in your reply -- "I tried to notify you but the
notification did not go out" -- so the user knows why they heard nothing;
never retry in a loop. A `could not check who is watching this chat` warning
is not a failure: the notification still went out, and the exit code says so.
