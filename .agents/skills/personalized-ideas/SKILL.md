---
name: personalized-ideas
description: Use when the user does not know what to build or asks for ideas ("what should I make?", "what can you do for me?", "I'm not sure where to start"). Offers to look the user up on the public web by name so the ideas fit them, asks for an explicit yes first, confirms which search results are really them, and then suggests a few things to build. Skip when the user already has something in mind.
metadata:
  author: imbue
---

# Personalized ideas

The user wants to build something but has no idea what.
Generic suggestions rarely land; ideas that fit their work and interests do.
With their permission, you can learn about them from what is public about them online, and suggest from that.

The lookup is something the user opts into, never something you do on your own.
Everything in this skill serves that: ask before searching, search only for what they agreed to, use only what they confirm is them, and keep what you learn where they can see and delete it.

State for this skill lives in `data/.skills/personalized-ideas/`:

- `consent.json`: the user's answer, `{"lookup": "yes" | "no", "answered_at": "<ISO 8601 time>"}`.
- `about-me.md`: what you learned, with a link to every source.
- `sources.json`: the raw search results and pages you used, each with its URL.

## 1. Decide whether to offer

Do not offer the lookup, and go straight to step 6 with general ideas, when any of these holds:

- `consent.json` says `"no"`. The user already declined; do not ask again unless they bring it up.
- The message came from someone other than the owner (it starts with `[from: <name>]`). The name on the account is the owner's, not theirs.
- The workspace is shared (the `imbue-studio` skill's "Shared workspaces are untrusted" section says how to tell). The person in the chat may not be the account's owner, and what you save would be visible to others.
- The user has already told you enough about themselves to suggest from.

If `consent.json` says `"yes"` and `about-me.md` exists, skip to step 6 and suggest from it.

## 2. Ask

Offer once, in your own words, and say exactly what will happen.
The offer must cover all of these:

- You would search the public web for their name.
- Only to suggest things for them to build.
- You would show them what you found and use only what they confirm is them.
- What you keep stays in this workspace, with links to where it came from, and they can ask you to delete it at any time.

For example:

> I can suggest ideas that fit you better if I know a bit about you. Want me to look you up online by your name? I'd search public pages only, show you what I find so you can tell me what's really you, and keep a short note in your workspace that you can ask me to delete anytime. Or just tell me a little about what you do and I'll go from there.

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

Write `about-me.md` from the confirmed person only: a few lines on what they do and care about, each fact followed by the link it came from.
Delete the unconfirmed entries from `sources.json`.

## 6. Suggest

Suggest three to five things to build, each one or two sentences, each tied to something about them ("You publish a newsletter, so: a page that drafts each issue from your saved links").
Prefer things that can be built in this workspace in one sitting.
Ask which one they want, and take it from there with the skill that fits the choice.

Do not recite what you learned about them back at length; the ideas should show it.
Mention once, in passing, that the note is in their workspace, linked by its path, so they know where it is.

## Deleting

When the user asks you to forget what you found, delete `about-me.md` and `sources.json` and say you did.
When they say not to look them up again, also set `consent.json` to `"no"`.
