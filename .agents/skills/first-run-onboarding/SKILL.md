---
name: first-run-onboarding
description: Use only when the context block at the top of a chat's first message says this is the user's first conversation in a new workspace and tells you to read this skill. Holds the exact reply to each start suggestion offered under the welcome's "How do you want to start?" ("Describe what you want to make", "Get ideas for what to make", "See what Studio can do"), and the scripted questions and idea lists that follow "Get ideas for what to make". Not for any other chat, and not once the scripted conversation has ended.
metadata:
  author: imbue
---

# The first conversation in a new workspace

A new workspace opens on a chat that already shows a welcome ending in "**How do you want to start?**", with suggested replies under it.
The user's first message answers that question, usually by clicking a suggestion, which sends the suggestion's words exactly.
The reply to a suggestion is scripted: this file holds its text, and you send it word for word.

The script starts at the message that arrived with the context block that sent you here.
Paths A and C end with your first reply.
Path B runs over the next few messages, and this file says where it ends.

## The start

Compare that first message, trimmed, with the suggestions:

| The message | What to do |
|---|---|
| `Describe what you want to make` | Path A |
| `Get ideas for what to make` | Path B |
| `See what Studio can do` | Path C |
| Anything else | An ordinary request: answer it as you would in any chat, and stop following this skill. |

The same words typed by hand count as the suggestion.
A message that holds more than the suggestion's words is "anything else".

## Every scripted reply

A scripted reply is conversation, not work:

- Create no progress steps for it (no `tk create --step`) and send no notification.
- Use no tools, except the one command path C names and the one file read path B names.
- Send the text as written here: the same words, headings, bold and list, with nothing before or after it.
- Do not mention this skill, or that the reply was prepared.

## Path A: "Describe what you want to make"

Reply with exactly this message:

```
## Describe the problem you want to solve. Provide lots of context!

What’s annoying, overwhelming, or tedious in your digital life? Go ahead and complain--I’ll help you build something that makes it better.

*Example: I feel scattered. My todos are in a million different places, and I can never find things when I need them. I want a command center that pulls everything into one place and tells me what to do next.*

**Prefer to talk? Tap the mic and think out loud.**
```

Then stop following this skill: what the user says next is an ordinary request.

## Path B: "Get ideas for what to make"

Two questions, then a list of ideas that fits both answers.
The chat draws a picker where a reply carries a picker tag, and offers suggestions under your replies on its own, so write no list of options yourself.

### B1: what the workspace is for

Reply with exactly this message, the tag on a line of its own:

```
**What do you want to use this Studio for?**

<studio_category_picker/>
```

The picker offers these categories, and the user's answer is the ones they picked, in the order they picked them, or words of their own:

- Work: Knowledge work, Team projects, Sales, Marketing, Operations, Recruiting, Fundraising, Small business
- Life: Personal, Money, Health, Home & family, Travel & events, Job search, School, Notes & archives
- Creative: Writing, Art & design, Film & video, Game dev, Books, Movies, Music

The pickers' "Other" pills put "Write whatever you want!" and "Write whatever data you want here!" in the message as prompts to write over, so either sentence arriving unchanged names no category and no app.

### B2: the apps they use

Whatever their answer to B1 names, reply with exactly this message, the tag on a line of its own:

```
**Which apps do you use most?**

We'll suggest things to build around them.

<studio_connector_picker/>
```

The picker offers Gmail, Google Calendar, GitHub, Slack, Notion, Google Drive, Google Sheets, Stripe, Instagram, YouTube, Strava, HubSpot, Asana, Todoist and Linear.
Nothing is connected at this step: the answer only tells you what to suggest.

### B3: the ideas

Their answer to B2 is the apps they use, or `Show me some ideas` alone, which means no apps.
An answer that names apps and ends with `Show me some ideas` names those apps.

Read `references/app-ideas.md` (beside this file), then send one message.
It opens with this line, their categories joined as a phrase in bold and their apps as a phrase:

```
Here are some ideas for what you might want to build for **<their categories>** with your <their apps>:
```

For example: "Here are some ideas for what you might want to build for **Sales and Money** with your Gmail, Google Calendar and Stripe:".
With no apps, leave " with your <their apps>" out.

Under it comes a bulleted list, one idea per item, each written exactly in this form:

```
- **<idea name>**: <one or two sentences on what it is and does> (<the apps of theirs it would use>).
```

The chat reads each idea's name from that form, the bold text that opens a list item, and offers it as a suggestion the user can click.
So the idea name is short (a few words, well under 80 characters), has no punctuation, is bold, and opens its item; use bold nowhere else in the list.
Leave the parentheses out for an idea that uses none of their apps.

Which ideas:

- One category: three ideas.
  Two or more: four, taking turns between the categories so each is represented, leading with the category they named first.
- A category of their own (not one of the listed ones): its first idea is made for exactly what they typed and named after it; the rest come from the closest listed category, or from Personal when none is close.
- An app of their own (not one of the listed ones): name it in the opening line and in the reply to a picked idea; do not repeat it on every idea, and do not let it change which ideas are shown.
- Prefer a clever, specific twist over a generic tracker, and vary the ideas.

The chat offers one suggestion per idea under the list, then `More ideas` and `Something else`.
What the user sends next decides the reply:

| The message | What to do |
|---|---|
| `More ideas` | Send five more ideas, none repeated, in the same item form, opening with exactly `A few more:`. The same three kinds of message can follow it. |
| The name of an idea you listed | Reply with the line below, then build it. |
| `Something else` | Reply with the two paragraphs below. |
| Anything else | An ordinary request: answer it, and stop following this skill. |

An idea picked:

```
Great. I'll start with a quick sketch of **<idea>** using your real <their apps> data, so you can steer it before I build the real thing.
```

Leave " using your real <their apps> data" out when none of their apps apply to the idea.
Then stop following this skill and build the idea as any app is built (the `build-app` skill), starting with its mock: from here it is work, with progress steps and a notification as usual.

`Something else`:

```
Sure. Describe the problem you'd like to solve.

Provide as much context as possible. Use the voice input button to just ramble!
```

Then stop following this skill: what the user says next is an ordinary request.

### When the user leaves the script

At B1 and B2 the answer is the user's own words, so go on to the next question even when it is not what you expected (a category of their own, an app you do not know, a shrug).
Only a message that is plainly something else, such as a question for you or a request to build a particular thing, ends the script: answer it as an ordinary request and stop following this skill.

## Path C: "See what Studio can do"

Open the Getting Started app on the left half of the desktop, in front, with the chat on the right half, which is where the tour puts the chat. Run these three commands in this order:

```bash
uv run --no-sync workspace-layout place self --state snapped-right
uv run --no-sync workspace-layout open getting-started
uv run --no-sync workspace-layout place getting-started --state snapped-left
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

If the `open` command failed, leave out the last paragraph, since nothing was opened, and do not retry it. A `place` command that failed only leaves a window where it was: say nothing about it and do not retry it.

Then stop following this skill: what the user says next is an ordinary request.
