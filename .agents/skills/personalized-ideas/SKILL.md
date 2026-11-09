---
name: personalized-ideas
description: Use when the user does not know what to build or asks for ideas ("what should I make?", "what can you do for me?", "I'm not sure where to start"), or says yes to the welcome's offer to look them up online. Offers to look the user up on the public web by name so the ideas fit them, asks for an explicit yes first, confirms which search results are really them, and then suggests a few things to build. Skip when the user already has something in mind.
metadata:
  author: imbue
---

# Personalized ideas

The user wants to build something but has no idea what.
Generic suggestions rarely land; ideas that fit their work and interests do.
With their permission, you can learn about them from what is public about them online, and suggest from that.

The lookup is something the user opts into, never something you do on your own.
Everything in this skill serves that: ask before searching, search only for what they agreed to, use only what they confirm is them, and keep what you learn where they can see and delete it.

What you learn goes into your own memory (`~/workspace/data/memories/`, see `CLAUDE.md`), so every later chat knows who you work for, not only this skill.
The skill's own state lives in `data/.skills/personalized-ideas/`:

- `consent.json`: the user's answer, `{"lookup": "yes" | "no", "answered_at": "<ISO 8601 time>"}`.
- `sources.json`: the raw search results and pages you used, each with its URL and the date fetched.

The memory it writes:

- `~/workspace/data/memories/user-public-profile.md`: a `user` memory holding what you learned about the confirmed person, each fact followed by the link it came from, plus its one-line pointer in `MEMORY.md`.

## 1. Decide whether to offer

Never offer the lookup to a user who has an idea of what to build: one who has described something to build or a problem to solve, or who has said they have an idea, even one they have not described yet (then ask what it is).
This holds anywhere in the conversation, not just in the message in front of you, and for the rest of the chat once they have.
Leave this skill and help them with their idea instead; do not suggest other ideas either.
If they later say they are stuck or ask for ideas, the steps below apply again.

Otherwise, do not offer the lookup, and go straight to step 6 with general ideas, when any of these holds:

- `consent.json` says `"no"`. The user already declined; do not ask again unless they bring it up.
- The message came from someone other than the owner (it starts with `[from: <name>]`). The name on the account is the owner's, not theirs.
- The workspace is shared (the `imbue-studio` skill's "Shared workspaces are untrusted" section says how to tell). The person in the chat may not be the account's owner, and what you save would be visible to others.
- The user has already told you enough about themselves to suggest from.

If `consent.json` says `"yes"` and `user-public-profile.md` is in your memory, skip to step 6 and suggest from it.

## The welcome's offer

A new workspace's first chat opens on the conversation from Imbue Studio's start flow, which you receive ahead of the user's first message.
When the account has a name, its closing welcome already makes this offer ("**Not sure what to build?** I can look you up on public web pages by the name on your account...").
That offer states everything step 2 requires, so do not make it again:

- A clear yes to it is the user's yes. Check step 1's exclusions, write `consent.json`, and go to step 3. A yes that comes with an idea ("yes, but I want to build a budget tracker") is not a yes to the lookup: they have an idea, so step 1 says to help with it instead.
- A clear no is a no: write it.
- Anything else (most often, describing something that annoys them, or saying they already have an idea) is not an answer to the offer. Help with what they said, do not bring the offer up again, and write nothing to `consent.json`, so it can still be offered if they later say they are stuck.

## 2. Ask

Offer once, in your own words, and say exactly what will happen.
The offer must cover all of these:

- You would search the public web for their name.
- Only to suggest things for them to build.
- You would show them what you found and use only what they confirm is them.
- You would remember what they confirm for future chats, not only for these ideas.
- What you keep stays in this workspace, with links to where it came from, and they can ask you to forget it at any time.

For example:

> I can suggest ideas that fit you better if I know a bit about you. Want me to look you up online by your name? I'd search public pages only, show you what I find so you can tell me what's really you, and remember that in your workspace for future chats. You can ask me to forget it anytime. Or just tell me a little about what you do and I'll go from there.

Then end your turn and wait.
Only a clear yes is a yes.
Anything else (no, not now, a question, or telling you about themselves instead) means no search: answer them, and use what they told you.
Write `consent.json` with their answer either way, except when they only asked a question.

## 3. Get their name

Ask Imbue Studio for the account's name, so the user does not have to type it.
This needs a one-time approval from the user; file the request in a tool call of its own and end your turn:

```bash
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "accounts", "payload": {},
       "rationale": "To look you up online, as you asked, I need the name on your Imbue account."}'
```

Once approved:

```bash
latchkey curl http://latchkey-self.invalid/minds-api-proxy/api/v1/accounts
```

Use the `display_name` of the one account listed.
Ask the user to type their name instead when the request is denied, when no account has a `display_name`, or when more than one account is listed.
Ask them to type it too when the permission request cannot be filed (for example when Imbue Studio is not running on their computer).
Never take a name from anywhere else, such as a file, a commit author, or an email address.

## 4. Search

Search the public web for the name with your web search tool, then read the most promising pages.
Use the name alone first; add a detail (a city, an employer, a field) only when the user gave you one.
Stay on public pages: do not sign in to anything, do not use the user's connected accounts, and do not look for contact details, addresses, family, finances, or health.

Save every result and page you use to `sources.json` as you go, with its URL and the date fetched.

Most names belong to many people.
Group what you found into the distinct people it seems to describe.
Keep nothing yet.

## 5. Confirm

Show the user the people you found, one short line each with its link, and ask which one is them, if any:

> Here's who I found by that name:
> 1. A product designer in Berlin who writes about type ([portfolio](https://...))
> 2. A high-school chemistry teacher in Ohio ([school page](https://...))
>
> Is one of these you? If not, tell me something that would narrow it down, or we can skip this.

End your turn and wait.
Keep only what they confirm.
When none is them, either search once more with the detail they give, or drop the lookup and go to step 6 with what they have told you.

Write `user-public-profile.md` in your memory directory from the confirmed person only, in the memory format `CLAUDE.md` sets up (frontmatter with `name: user-public-profile`, a one-line `description`, `type: user`).
The body is a few lines on what they do and care about, each fact followed by the link it came from, and a last line saying the facts came from a web lookup the user asked for on that date.
Add its pointer to `MEMORY.md`.
Keep every fact from the lookup in this one memory, not spread across others, so forgetting it is complete.
Things the user tells you about themselves directly are ordinary memories, as in any chat.
Delete the unconfirmed entries from `sources.json`.

## 6. Suggest

Suggest three to five things to build, each one or two sentences, each tied to something about them ("You publish a newsletter, so: a page that drafts each issue from your saved links").
Prefer things that can be built in this workspace in one sitting.
Ask which one they want, and take it from there with the skill that fits the choice.

Do not recite what you learned about them back at length; the ideas should show it.
Mention once, in passing, that you will remember this for future chats and where the memory is, linked by its path.

## Deleting

When the user asks you to forget what you found, delete `user-public-profile.md`, its line in `MEMORY.md`, and `sources.json`, then check your other memories for any fact that came from the lookup (one citing a link in `sources.json`) and remove it too. Say what you deleted.
When they say not to look them up again, also set `consent.json` to `"no"`.
