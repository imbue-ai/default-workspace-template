---
name: handoff-summary
description: Write the summary the next agent needs to continue this chat when the chat app moves it to another harness. Invoked by the chat app as a slash command naming the output path, never by the user.
metadata:
  author: imbue
---

# Handoff summary

The chat app is about to move this conversation to a new agent on another
harness. That agent starts with none of your context: the chat app puts the
file you write here into its first message (a file over 64 KB is pointed at
instead, and the agent is told to read it first), so it reads it before
anything else. The point is to make the two sessions read as one long chat.

The slash command carries the output path, for example
`/handoff-summary data/.apps/chat/chats/agent-abc/summaries/1.md`. Write the
file there, in markdown, and stop. Do not reply to the user, and do not mention
any of this unless directly asked about it: the switch is automated, and a
message about it would only confuse them.

## What to write

Sections, in order:

1. **What the user is trying to do right now**, in their words.
2. **What has been done so far, and what is in progress at this moment.**
3. **Decisions taken, and why.**
4. **Important files, branches, and workers touched**, with paths and worker
   ids.
5. **Questions the user has not answered.**
6. **What the user is waiting for next.**
7. **Anything a subagent or worker was doing when you stopped.**

Make sure the summary contains every detail the next agent needs to pick up
where you left off: pointers to an earlier summary if you wrote one, what has
changed recently, any hints or tips.

**Be sure to include any important details that have not been addressed yet.**
Do not drop anything the user might care about unless they said it does not
matter, or, given later information, it obviously no longer does.

Err on the side of including a lot of extra detail.

## Rules

- Write for an agent on a different harness with none of your context.
- Prefer concrete paths, ids, and commands over descriptions.
- Never include secrets or credentials.
- Write the whole file in one go; the chat app proceeds as soon as it sees a
  non-empty file.
- If you cannot write the file, do nothing else: the next agent gathers its own
  context from your transcript.
