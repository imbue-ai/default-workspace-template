---
name: suggest-apps
description: The app-data part of the personalized-ideas skill. Use when personalized-ideas sends you here, or when the user asks for app ideas from their Mac's apps ("look at my apps and suggest something"). For a user who simply has no idea what to build, load personalized-ideas first: it leads that conversation. Suggests a few apps that fit how the user actually works, from their Mac's app data when they allow it, from a few quick questions when they don't. Not for a request that already names what to build, and not for a bare greeting.
compatibility: Uses the read-app-data skill to ask for and read the user's Mac app data, and build-app to build the idea they pick.
metadata:
  author: imbue
---

# Suggesting apps to build

The user wants something built and has no idea what. Suggest three to five apps
that fit how they actually work, then build the one they pick. Their Mac's app
data is the best evidence of how they work, so ask to read a little of it first.

## When this applies

The `personalized-ideas` skill leads whenever the user has no idea what to build,
and comes here for the app data: follow the steps it names. Its offer and its
welcome handling come first; never make a separate offer to read app data.

- The user says they have no idea what to build, or asks you for ideas.
- The user asks you to build or make something but names nothing specific.

It does not apply when the request names what to build (build that, with
`build-app` or `do-something-new`), or to a greeting with no request in it.

## 1. Use what you can already read

Check your existing grants first (the `read-app-data` skill, "When to ask"). If
you already hold sources from the list below, read them and skip to step 3. Ask
only for the ones you do not hold yet.

## 2. Ask for everyday sources, once

Check whether Imbue Studio has Full Disk Access on the user's computer, with
`X-Latchkey-Device` set to that device:

```bash
latchkey curl -H "X-Latchkey-Device: <device ID>" http://latchkey-self.invalid/minds-api-proxy/api/v1/full-disk-access
```

Then file one `app-data` request (the `read-app-data` skill, "Asking") for:

- `installed-apps` and `browser-history`, always;
- `notes`, `reminders`, and `calendar` too, when the status is `ON` or `is_asking_allowed` is true: these need
  Full Disk Access, and the request's dialog walks the user through turning it on.

Never ask for `messages`, `shell-history`, or `code-projects` here: they are the
most private sources, or the most likely to name clients, and suggestions do not
need them. Ask for one only when the user brings it up themselves ("look at my
projects too").

The reason names the task in one plain sentence, for example: "To suggest apps
that fit how you work, I'd like to see which apps you have installed and the
sites you visit most." Then wait for the answer before telling the user anything
about the request: Imbue Studio may answer it at once (a shared workspace is
refused without a dialog). If no answer has come, tell the user, in one sentence,
that they can untick anything in the request.

## 3. Read for patterns, not details

Read what was granted as the `read-app-data` skill describes (copy each database
with its `-wal` and `-shm`, and read the copy). Look for what the user does
repeatedly, not for anything particular:

- **Installed apps:** the tools they rely on (an editor, a design tool, a finance
  app, a music app).
- **Browser history:** the sites they visit most, by visit count, and the kind of
  work or interest each stands for.
- **Notes, Reminders, Calendar:** recurring topics, chores, and meetings: what
  keeps coming back.

Keep it at that level. Never put raw history, note text, event titles, or
people's names into the chat or into a suggestion. Leave out anything about
health, money, relationships, or other people, even when it is the strongest
signal. Delete your copies when you have your patterns.

## 4. Suggest three to five apps

For each suggestion give:

- **a plain name**, and one sentence on what the app does;
- **why it fits you**, naming the pattern behind it in general terms ("you open
  Linear and GitHub every day"), never a private detail;
- **how big it is**: a quick page, or something that takes a while.

Prefer apps that save the user a chore they repeat, or bring together things they
check in several places. Make them different from one another. Then ask which one
to build, and say they can change any of them.

When they pick one, build it with the `build-app` skill (or route through
`do-something-new` when it is more than a page).

## When there is no data to go on

The user declines the request, unticks everything, or nothing could be read (no
Full Disk Access for the sources that need it, and nothing else granted): do not
ask for app data again in this task. Ask two or three quick questions instead:

- what their work or main project is;
- what they do every day or every week that feels repetitive;
- what they check in several places.

Then suggest three to five apps from the answers, as in step 4, saying they are
based on what the user told you.

## Only for the owner

Suggestions drawn from app data are for the workspace's owner. Follow the
`read-app-data` skill's "Only for the owner": never make data-based suggestions
in answer to a message with a `[from: <name>]` prefix; use the questions above
instead.
