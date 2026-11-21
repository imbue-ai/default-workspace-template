---
name: first-run-onboarding
description: Use only when the context block at the top of a chat's first message says this is the user's first conversation in a new workspace and tells you to read this skill. Holds the exact reply to each start suggestion offered under the welcome's "How do you want to start?" ("Describe what you want to make", "See what Studio can do"). Not for any other chat, and not for later messages in that one.
metadata:
  author: imbue
---

# The first conversation in a new workspace

A new workspace opens on a chat that already shows a welcome ending in "**How do you want to start?**", with suggested replies under it.
The user's first message answers that question, usually by clicking a suggestion, which sends the suggestion's words exactly.
The reply to a suggestion is scripted: this file holds its text, and you send it word for word.

This applies to one message only: the one that arrived with the context block that sent you here.

## The start

Compare that message, trimmed, with the suggestions:

| The message | What to do |
|---|---|
| `Describe what you want to make` | Path A |
| `See what Studio can do` | Path C |
| Anything else | An ordinary request: answer it as you would in any chat, and stop following this skill. |

The same words typed by hand count as the suggestion.
A message that holds more than the suggestion's words is "anything else".

## Every scripted reply

A scripted reply is conversation, not work:

- Create no progress steps for it (no `tk create --step`) and send no notification.
- Use no tools, except the one command path C names.
- Send the text as written here: the same words, headings, bold and list, with nothing before or after it.
- Do not mention this skill, or that the reply was prepared.

## Path A: "Describe what you want to make"

Reply with exactly this message:

```
## Describe the problem you want to solve. Provide lots of context!

Describe something your current apps don't quite do, a task that takes too long, or any other problem you're facing right now.

**Prefer to talk? Tap the mic and think out loud.**
```

Then stop following this skill: what the user says next is an ordinary request.

## Path C: "See what Studio can do"

Open the Getting Started app beside the chat:

```bash
uv run --no-sync workspace-layout open getting-started --beside
```

Then reply with exactly this message:

```
Sure! Here is a list of some of the features in Studio:

- **Apps**: describe what you need and I'll build it. Each app opens as a window here and works on your phone too.
- **Your data**: connect Gmail, Slack, Notion, files on your computer, and hundreds of other services. Your logins stay secure.
- **Routines**: have me do something on a schedule, like a summary of your inbox every morning.
- **Parallel work**: start as many chats as you like, and hand long tasks to background agents.
- **Sharing**: share the whole workspace, or a single app, with your team, friends, or family.
- **Any AI**: use the provider you prefer, and switch between them, even in the middle of a chat.
- **Yours to change**: everything here can be customized, from the apps to the desktop itself, and it is backed up automatically.

Feel free to ask me for more details about any of them!

I've also opened the **Getting Started** app next to our chat. It has some simple ways to get started, as well as templates and apps other people built that you can get started with immediately or use as a starting point for your own apps and ideas.
```

If the command failed, leave out the last paragraph, since nothing was opened, and do not retry it.

Then stop following this skill: what the user says next is an ordinary request.
