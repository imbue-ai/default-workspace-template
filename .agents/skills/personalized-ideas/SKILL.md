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
When the account has a name, its closing welcome already makes this offer ("**Not sure what to build? I’d like to get to know you a little first,** ...").
That offer states everything step 2 requires, so do not make it again:

- `yes` (or another clear yes, such as "sure" or "go ahead") is the user's yes. Check step 1's exclusions, write `consent.json`, and go to step 3. A yes that comes with an idea ("yes, but I want to build a budget tracker") is not a yes to the lookup: they have an idea, so step 1 says to help with it instead.
- `no` (or another clear no, such as "no thanks" or "skip it") is a no: write it.
- Anything else (most often, describing something that annoys them, or saying they already have an idea) is not an answer to the offer. Help with what they said, do not bring the offer up again, and write nothing to `consent.json`, so it can still be offered if they later say they are stuck.

## 2. Ask

Make the offer once, as this exact message, word for word, with nothing added before or after it:

> **Not sure what to build? I’d like to get to know you a little first,** so I can suggest things that actually help you. If you’re comfortable with it, I can search public web pages for the name on your account, using an outside search service. I’ll show you what I find so you can tell me what’s really you, and I’ll only remember what you confirm, in this workspace, for future chats. You can ask me to forget it anytime, or skip this and just tell me a bit about yourself. Reply **yes** to go ahead, or **no** to skip it.

It is the same message Imbue Studio's welcome uses (`LOOKUP_OFFER` in mngr-internal's `apps/minds/frontend/src/models/creationTranscript.ts`), and it is fixed on purpose: one wording, reviewed once, that says what the lookup does without sounding like you already know something about them.
Do not reword it, shorten it, personalize it, or mention anything about the user in it; change it only in both places at once.

Then end your turn and wait.
Only `yes`, or another clear yes, is a yes; `no`, or another clear no, is a no.
Anything else (no, not now, a question, or telling you about themselves instead) means no search: answer them, and use what they told you.
Write `consent.json` with their answer either way, except when they only asked a question.

## 3. Get their name

Do this in the same turn as the user's yes, with no question and no approval in between: the offer they accepted names "the name on your account", so read it straight away from Imbue Studio:

```bash
latchkey curl -sS "http://latchkey-self.invalid/minds-api-proxy/api/v1/agents/$MNGR_AGENT_ID/owner-name"
```

The answer is `{"display_name": "<name>"}` for the account that owns this workspace.
Ask the user to type their name instead when `display_name` is null or the call fails (for example when Imbue Studio is not running on their computer), and end your turn.
Never take a name from anywhere else, such as a file, a commit author, or an email address, and never ask for the accounts permission for this: that route is all the lookup needs.

## 4. Search

Search with your own web search tool, and read pages with your web fetch tool.
That is the whole lookup: there is nothing for the user to connect, sign up for, or pay.

Go straight from the name to the results, and keep it quick: the user is waiting on this turn.

- **Search in parallel.** Run every search in one batch of tool calls rather than one after another: the name alone, and, when the user gave you a detail (a city, an employer, a field), the name with that detail. At most 3 searches.
- **Show options from the search results alone.** Do not read any page before the user confirms: titles, snippets and links are enough to tell the people apart and to ask which is them. Pages are read in step 5, only for the person they confirm.

**Sign-ins.** Some results, LinkedIn profiles most often, show only part of a page until someone signs in.
Use such a result only as far as its search result (title and snippet) shows, and do not try to read the page itself: it would answer a sign-in wall, not the profile.
Never sign in to anything, and never use the user's own connected accounts (their LinkedIn included) to see more.
If that profile matters, say so when you show the results in step 5, and offer the user's own way to share it: paste the parts of it they are happy to share, or upload the PDF LinkedIn can make of their own profile (**Save to PDF**, in the menu on their profile page).
What they share themselves goes in `sources.json` as coming from them, with no URL needed.

Read only pages that are public: do not look for contact details, addresses, family, finances, or health, even when a page offers them.

Save every result you use to `sources.json` as you go, and every page you later read, with its URL and the date fetched.

Most names belong to many people.
Group the results into the distinct people they seem to describe, from the titles and snippets.
Keep nothing yet.

### Why only a web search

Services exist that look a person up in people-search or sales-lead databases (Lusha, People Data Labs, Apollo, Hunter and the like, some of them reachable through tool brokers such as treg): they return work emails, phone numbers, social profiles and employment history for a name.
This skill does not use them, on purpose:

- **It would seem creepy.** The offer promises a look at public web pages. Being shown your own phone number or email history from a database you never gave it to is the opposite of what the user agreed to, and the reason the offer is worded as it is.
- **It is often wrong.** Their matches are guesses (treg calls a found email or phone "the provider's best match, not a confirmed one"), so they attach another person's details to the user as easily as their own.
- **It is data you must not collect.** Contact details are excluded above, and gathering them about a person from brokers carries notice duties (for example GDPR Article 14) that a public search with the user watching does not.

## 5. Confirm

Show the user the people you found, one short line each with its link, and ask which one is them, if any:

> Here's who I found by that name:
> 1. A product designer in Berlin who writes about type ([portfolio](https://...))
> 2. A high-school chemistry teacher in Ohio ([school page](https://...))
>
> Is one of these you? If not, tell me something that would narrow it down, or we can skip this.

End your turn and wait.
Keep only what they confirm.

Once they confirm one, read up to 5 of that person's pages from the results, all in one batch of tool calls, to fill in what they do and care about; skip any page behind a sign-in (see "Sign-ins" above).
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
