---
name: personalized-ideas
description: Use when the user does not know what to build or asks for ideas ("what should I make?", "what can you do for me?", "I'm not sure where to start"), or answers no to the welcome's "Do you have any ideas?". Offers to look the user up on the public web by name so the ideas fit them, asks for an explicit yes first, confirms which search results are really them, and then suggests a few things to build. Skip when the user already has something in mind.
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

- `consent.json`: the user's answers, `{"lookup": "yes" | "no", "answered_at": "<ISO 8601 time>", "only_me": true, "shared_ok": true, "email_org": "yes" | "no"}` (`only_me` is present once they have confirmed, while sharing is on, that nobody else uses this workspace, and `shared_ok` once they have said others can but to search anyway, see step 3; `email_org` once they have answered whether to search with their email's organization, see step 5).
- `sources.json`: the raw search results and pages you used, each with its URL and the date fetched.

The memory it writes:

- `~/workspace/data/memories/user-public-profile.md`: a `user` memory holding what you know about the user, each fact followed by the link it came from or `(you told me)`, plus its one-line pointer in `MEMORY.md`. It is what the **What Studio knows about me** page shows, so every fact about the user goes here: those from a lookup they confirmed, those they tell you in chat (see "What the user tells you" below), and those they add on the page.

## 1. Decide whether to offer

Never offer the lookup to a user who has an idea of what to build: one who has described something to build or a problem to solve, or who has said they have an idea, even one they have not described yet (then ask what it is).
This holds anywhere in the conversation, not just in the message in front of you, and for the rest of the chat once they have.
Leave this skill and help them with their idea instead; do not suggest other ideas either.
If they later say they are stuck or ask for ideas, the steps below apply again.

Otherwise, do not offer the lookup, and go straight to step 6 with general ideas, when any of these holds:

- `consent.json` says `"no"`. The user already declined; do not ask again unless they bring it up.
- The message came from someone other than the owner (it starts with `[from: <name>]`). The name on the account is the owner's, not theirs.
- The user has already told you enough about themselves to suggest from.

If `consent.json` says `"yes"` and `user-public-profile.md` is in your memory, skip to step 6 and suggest from it.

## The welcome's question

A new workspace's first chat opens on the conversation from Imbue Studio's start flow, which you receive ahead of the user's first message.
Its closing welcome asks "The best way to start is to build something. Do you have any ideas?", with two reply buttons that send a whole sentence:

- `Yes, I have an idea` (or another clear yes): they have an idea, so step 1 applies. Ask what it is, and help with it; do not offer the lookup.
- `No, I don’t have an idea yet` (or another clear no): check step 1's exclusions, then read the name on the account (the `owner-name` call in step 3; it reads only the name, and searches nothing). When none of the exclusions holds and `display_name` is set, make the offer in step 2. Otherwise send only its first paragraph, the question about what is annoying them, and nothing else. Either way, end your turn.
- Anything else (most often, describing what they want to build or something that annoys them): help with what they said, and do not offer the lookup.

## 2. Ask

Make the offer once, as this exact message, word for word, with nothing added before or after it (its last line is two reply buttons; the chat turns `#reply=` links into buttons that send their text for the user):

> What’s annoying, overwhelming, or tedious in your digital life? Work or personal, big or small, tell me about it and I’ll help you build something that makes it better.
>
> Or I can get to know you a little first, so I can suggest things that actually help you. If you’re comfortable with it, I can search public web pages for the name on your account, using an outside search service. I’ll show you what I find so you can tell me what’s really you, and I’ll only remember what you confirm, in this workspace, for future chats. You can ask me to forget it anytime.
>
> [Search for me](#reply=Search%20for%20me) [I’ll tell you myself](#reply=I%E2%80%99ll%20tell%20you%20myself)

It is fixed on purpose: one wording, reviewed once, that says what the lookup does without sounding like you already know something about them.
Do not reword it, shorten it, personalize it, or mention anything about the user in it.
Outside the welcome (the user asks for ideas later in a chat), make the same offer only when the `owner-name` call answers a `display_name`; without one, ask the first paragraph's question alone.

Then end your turn and wait.

- `Search for me`, or another clear yes to the search: write `"lookup": "yes"` to `consent.json` and go to step 3.
- `I’ll tell you myself`, or another clear no to the search: write `"lookup": "no"`, and ask them to go ahead in one short line. What they then tell you about themselves is saved as in "What the user tells you" below.
- Anything else (describing what annoys them, a question): no search. Answer them and use what they told you; write `consent.json` only when they clearly declined the search.

## 3. Get their name

Read the name in the same turn as the user's yes, with no question and no approval before it (when you already read it to decide whether to offer, use that answer): the offer they accepted names "the name on your account", so read it straight away from Imbue Studio:

```bash
latchkey curl -sS "http://latchkey-self.invalid/minds-api-proxy/api/v1/agents/$MNGR_AGENT_ID/owner-name"
```

The answer is `{"display_name": "<name>", "email_domain": "<domain>"}` for the account that owns this workspace.
`email_domain` is the domain of the owner's email only when it names an organization (`imbue.com`); it is null for personal mail providers such as Gmail, and the address itself is never answered. Keep it for step 5: it ranks the results, and it is never searched with unless the user says yes to the question there.
Ask the user to type their name instead when `display_name` is null or the call fails (for example when Imbue Studio is not running on their computer), and end your turn.
Never take a name from anywhere else, such as a file, a commit author, or an email address, and never ask for the accounts permission for this: that route is all the lookup needs.

### Check who can see the workspace

Before searching, check whether sharing is turned on for this workspace (the `imbue-studio` skill's "Shared workspaces are untrusted" section says how to tell).
Sharing is often on only so the owner can reach the workspace from their own phone or browser, so it is a question for the user, not a reason to give up.

- **Sharing is off**, or `consent.json` already has `"only_me": true` or `"shared_ok": true`: go straight to step 4, with no question.
- **Sharing is on** and they have not confirmed yet: send exactly this message, word for word, then end your turn:

> Sharing is turned on for this workspace, so other people may be able to get into it. If they can, they would also see what I save about you, so I need to check with you first. Can anyone besides you use this workspace?
>
> [Only me, go ahead](#reply=only%20me) [Others can, but go ahead](#reply=others%20can%2C%20go%20ahead) [Skip the search](#reply=skip%20the%20search)

- `only me` (or another clear "only me"): add `"only_me": true` to `consent.json` and go to step 4. Do not ask again in later chats.
- `others can, go ahead` (or another clear "others can use it, and search anyway"): add `"shared_ok": true` to `consent.json` and go to step 4. Do not ask again in later chats. In step 6, also say that people who can use this workspace can see the **What Studio knows about me** page.
- `skip the search`, "not sure", or anything that is not a clear go-ahead (a bare "others can" is not one): set `consent.json`'s `lookup` to `"no"`, do not search, and go to step 6 with what they tell you about themselves.

## 4. Search

Search with your own web search tool, and read pages with your web fetch tool.
That is the whole lookup: there is nothing for the user to connect, sign up for, or pay.

Go straight from the name to the results, and keep it quick: the user is waiting on this turn.

- **Search in parallel.** Run every search in one batch of tool calls rather than one after another: the name alone, and, when the user gave you a detail (a city, an employer, a field), the name with that detail. At most 3 searches.
- **Only the name and what the user told you.** A search may contain the name and details the user stated in this conversation, nothing else. Never add anything you know from elsewhere: not their email address, not the workspace's name, files, git history or connected accounts, and not anything from earlier memories. The one exception is the organization behind `email_domain`, and only after the user says yes to including it (step 5). Searching with anything else looks the user up with information they never offered.
- **Widen a thin first batch.** If the first batch shows one person or none, run one more batch at once, before asking anything: the name with `LinkedIn`, with `GitHub`, and with `personal website` (at most 3 more searches). Most names belong to several people, and a single candidate is more often a thin result than a match.
- **Show options from the search results alone.** Do not read any page before the user confirms: titles, snippets and links are enough to tell the people apart and to ask which is them. Pages are read in step 5, only for the person they confirm.

**Sign-ins.** Some results, LinkedIn profiles most often, show only part of a page until someone signs in.
Use such a result only as far as its search result (title and snippet) shows, and do not try to read the page itself: it would answer a sign-in wall, not the profile.
Never sign in to anything, and never use the user's own connected accounts (their LinkedIn included) to see more.
If that profile matters, say so when you show the results in step 5, and offer the user's own way to share it: paste the parts of it they are happy to share, or upload the PDF LinkedIn can make of their own profile (**Save to PDF**, in the menu on their profile page).
What they share themselves goes in `sources.json` with `"origin": "user"` and a null `url`, and its facts end with `(you told me)` in the memory.

Read only pages that are public: do not look for contact details, addresses, family, finances, or health, even when a page offers them.

Save every result you use to `sources.json` as you go, and every page you later read: a JSON array of `{"url", "title", "fetched_at": "YYYY-MM-DD", "origin": "search" | "page" | "user"}` (what the user shares themselves has `"origin": "user"` and a null `url`).

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

**Rank by the email's organization.** When `email_domain` is set, list first any person whose title, snippet or link mentions that organization (its domain, or its name: `Imbue` for `imbue.com`), and say why in that line: "mentions Imbue, your email's organization". This only orders what the search already found; it sends nothing new.

**Offer to search with it.** Only when the name searches (the widened batch included) found no one at all, `email_domain` is set, and `consent.json` has no `"email_org"` answer yet: say you found no one by that name, and add this to the end of the message, with the organization's readable name in place of `<Org>` and its domain in place of `<domain>`:

> Your account email is at <domain>. Want me to also search for your name with <Org>?
>
> [Yes, include <Org>](#reply=include%20my%20organization) [No, don't](#reply=don%27t%20include%20it)

- `include my organization` (or another clear yes): set `"email_org": "yes"` in `consent.json`, run one search for the name with `<Org>` (never the address), and show the people again, ranked as above.
- `don't include it`, or a pick or detail that answers the question another way: set `"email_org": "no"` in `consent.json` and never search with it.

End your turn and wait.
Keep only what they confirm.

Once they confirm one, read up to 5 of that person's pages from the results, all in one batch of tool calls, to fill in what they do and care about; skip any page behind a sign-in (see "Sign-ins" above).
When none is them, either search once more with the detail they give, or drop the lookup and go to step 6 with what they have told you.

Write `user-public-profile.md` in your memory directory from the confirmed person only, in the memory format `CLAUDE.md` sets up (frontmatter with `name: user-public-profile`, a one-line `description`, `type: user`).
The body is one fact per line, in exactly this shape, so the "What Studio knows about me" page can show and delete each one:

```markdown
- <fact> ([<source title>](<url>)) <!-- found YYYY-MM-DD -->
- <fact the user shared themselves> (you told me) <!-- found YYYY-MM-DD -->
```

A few lines on what they do and care about, then a last plain line (not starting with `- `) saying the facts came from a web lookup the user asked for on that date.
The file may already exist, holding facts the user added on the **What Studio knows about me** page (`(you told me)` lines): keep every line already in it, add yours after its last fact, and do not duplicate one they already gave.
Add its pointer to `MEMORY.md`, unless it already has one.
Keep every fact from the lookup in this one memory, not spread across others, so forgetting it is complete.

## What the user tells you

When the user tells you about themselves (what they do or study, where they are, what they care about, what keeps annoying or overwhelming them), whether after **I’ll tell you myself**, while answering what annoys them, or anywhere else in a chat, save each such fact as one line of `user-public-profile.md`, so they can see, edit and delete it on the **What Studio knows about me** page:

```markdown
- <fact about them, in a short plain sentence> (you told me) <!-- found YYYY-MM-DD -->
```

- Only facts about the person, not details of what they are building or asking you to do ("wants a grading app" is not one; "grades 20 problem sets a week" is).
- Do not add a fact the file already holds; when they correct one, change that line rather than adding another.
- When the file does not exist yet, create it with the frontmatter above and add its pointer to `MEMORY.md`. When `sources.json` has no entry with `"origin": "user"`, add `{"url": null, "title": "What you told me", "fetched_at": "YYYY-MM-DD", "origin": "user"}`.
- Do not keep these facts in other memories too; this file is the one place for them.
- Every time you save or change facts, say so at the end of that reply, naming the page and linking it (its `link` from `uv run --no-sync workspace-layout list`), with exactly this sentence:

  > I’ve added what you told me about yourself to [What Studio knows about me](<link>), where you can edit or delete it.

  (When you changed a fact rather than adding one, say "updated" in place of "added".) Never save a fact about the user without that sentence in the same reply.
Delete the unconfirmed entries from `sources.json`.

## 6. Suggest

Suggest three to five things to build, each one or two sentences, each tied to something about them ("You publish a newsletter, so: a page that drafts each issue from your saved links").
Prefer things that can be built in this workspace in one sitting.
Ask which one they want, and take it from there with the skill that fits the choice.

Do not recite what you learned about them back at length; the ideas should show it.
Mention once, in passing, that you will remember this for future chats, and link the **What Studio knows about me** page (its `link` from `uv run --no-sync workspace-layout list`), where they can see each fact with its source, add facts about themselves, and edit or delete any of it.

## Deleting

When the user asks you to forget what you found, remove the lines of `user-public-profile.md` that cite a link, and `sources.json`'s entries that are not `"origin": "user"`, keeping the `(you told me)` lines; when no fact line is left, delete the file, its line in `MEMORY.md`, and `sources.json`. Then check your other memories for any fact that came from the lookup (one citing a link in `sources.json`) and remove it too.
When they ask you to forget everything about them, delete `user-public-profile.md`, its line in `MEMORY.md`, and `sources.json`, as the page's "Forget all of this" does.
Say what you deleted.
When they say not to look them up again, also set `consent.json` to `"no"`.
