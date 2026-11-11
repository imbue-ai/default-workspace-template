---
name: personalized-ideas
description: Use when the user does not know what to build or asks for ideas ("what should I make?", "what can you do for me?", "I'm not sure where to start"), or answers no to the welcome's "Do you have any ideas?". Offers to look the user up on the public web by name so the ideas fit them, asks for an explicit yes first, confirms which search results are really them, lets them choose between their Mac's app data (through suggest-apps), the web search, both at once, or telling you themselves, and then suggests a few things to build. Skip when the user already has something in mind.
metadata:
  author: imbue
---

# Personalized ideas

The user wants to build something but has no idea what.
Generic suggestions rarely land; ideas that fit their work and interests do.
With their permission, you can learn about them from what is public about them online, and from their Mac's app data (the `suggest-apps` and `read-app-data` skills), and suggest from that.
This skill leads the conversation; `suggest-apps` is the part of it that reads app data, and only when a step below sends you there.

The lookup is something the user opts into, never something you do on your own.

**No reply buttons.** Ask every question in plain text and let the user answer in their own words. Never put `#reply=` links (reply buttons) in your messages, anywhere in this skill: free-form answers are preferred, and a typed answer can say more than a button. Read each answer for its meaning, not for exact wording.
Everything in this skill serves that: ask before searching, search only for what they agreed to, use only what they confirm is them, and keep what you learn where they can see and delete it.

What you learn goes into your own memory (`~/workspace/data/memories/`, see `CLAUDE.md`), so every later chat knows who you work for, not only this skill.
The skill's own state lives in `data/.skills/personalized-ideas/`:

- `consent.json`: the user's answers, `{"choice": "both" | "mac" | "web" | "self", "lookup": "yes" | "no", "answered_at": "<ISO 8601 time>", "only_me": true, "shared_ok": true, "email_org": "yes" | "no"}` (`only_me` is present once they have confirmed, while sharing is on, that nobody else uses this workspace, and `shared_ok` once they have said others can but to search anyway, see step 3; `email_org` once they have answered whether to search with their email's organization, see step 5). `choice` is what they chose in step 2; `lookup` is `"yes"` for `both` and `web`. `app_terms` is `{"approved": [...], "declined": [...]}`, the search details from their app data they picked or turned down ("Search details from their Mac" in step 5).
- `sources.json`: the raw search results and pages you used, each with its URL and the date fetched.

The memory it writes:

- `~/workspace/data/memories/user-public-profile.md`: a `user` memory holding what the lookup found about the confirmed person, each fact followed by the link it came from, plus its one-line pointer in `MEMORY.md`. What the user tells you directly goes in ordinary memories instead (see "What the user tells you" below); the **What Studio knows about me** page shows both.

## 1. Decide whether to offer

Never offer the lookup to a user who has an idea of what to build: one who has described something to build or a problem to solve, or who has said they have an idea, even one they have not described yet (then ask what it is).
This holds anywhere in the conversation, not just in the message in front of you, and for the rest of the chat once they have.
Leave this skill and help them with their idea instead; do not suggest other ideas either.
If they later say they are stuck or ask for ideas, the steps below apply again.

Otherwise, do not offer the lookup, and go straight to step 6 with general ideas, when any of these holds:

- `consent.json` already has a `choice`. Do not ask again unless they bring it up: follow what they chose (for `mac`, suggest from the app data; for `self`, from what they told you).
- The message came from someone other than the owner (it starts with `[from: <name>]`). The name on the account is the owner's, not theirs.
- The user has already told you enough about themselves to suggest from.

If `consent.json` says `"yes"` and `user-public-profile.md` is in your memory, skip to step 6 and suggest from it.

## The welcome's question

A new workspace's first chat opens on the conversation from Imbue Studio's start flow, which you receive ahead of the user's first message.
Its closing welcome asks "The best way to start is to build something. Do you have any ideas?". Imbue Studio may show Yes and No buttons under it that send a whole sentence; the user may also type an answer:

- `Yes, I have an idea` (or another clear yes): they have an idea, so step 1 applies. Ask what it is, and help with it; do not offer the lookup.
- `No, I don’t have an idea yet` (or another clear no): check step 1's exclusions, then read the name on the account (the `owner-name` call in step 3; it reads only the name, and searches nothing). When none of the exclusions holds, make the offer in step 2 (the version without the web search when `display_name` is null). When one holds, send only its first paragraph, the question about what is annoying them, and nothing else. Either way, end your turn.
- Anything else (most often, describing what they want to build or something that annoys them): help with what they said, and do not offer the lookup.

## 2. Ask

Make the offer once, as this exact message, word for word, with nothing added before or after it, and no buttons:

> What’s annoying, overwhelming, or tedious in your digital life? Work or personal, big or small, tell me about it and I’ll help you build something that makes it better.
>
> Or I can get to know you a little first, so I can suggest things that actually help you. If you’re comfortable with it, I can look at a little of your Mac’s app data, like which apps you use and the sites you visit most, after you approve exactly what; it stays in this workspace. I can also search public web pages for the name on your account, using an outside search service, and show you what I find so you can tell me what’s really you. Pick either, both, or neither. I’ll only remember what you confirm, in this workspace, for future chats. You can ask me to forget it anytime.
>
> Just tell me which you’d like: your Mac apps and the web, only your Mac apps, only the web, or you’d rather tell me about yourself.

When `display_name` is null, send this version instead, word for word:

> What’s annoying, overwhelming, or tedious in your digital life? Work or personal, big or small, tell me about it and I’ll help you build something that makes it better.
>
> Or I can get to know you a little first, so I can suggest things that actually help you. If you’re comfortable with it, I can look at a little of your Mac’s app data, like which apps you use and the sites you visit most, after you approve exactly what; it stays in this workspace. I’ll only remember what you confirm, in this workspace, for future chats. You can ask me to forget it anytime.
>
> Just tell me if you’d like me to look at your Mac apps, or if you’d rather tell me about yourself.

It is fixed on purpose: one wording, reviewed once, that says what each choice does without sounding like you already know something about them.
Do not reword it, shorten it, personalize it, or mention anything about the user in it.
Outside the welcome (the user asks for ideas later in a chat), make the same offer.

Then end your turn and wait. Read their answer for which of these they mean, and write their choice to `consent.json` as soon as they answer. When it is unclear which they mean, ask once in plain words; never guess toward more data than they asked for:

- Both, their Mac apps and the web (`"choice": "both"`, `"lookup": "yes"`): both at once, in this same turn. In one batch of tool calls, file the app-data request ("Get the app data" below) and start the web lookup at step 3. The search does not wait for the app data, and the app data does not wait for the search. Search details from the app data are used as "Search details from their Mac" in step 5 says.
- Only their Mac apps (`"choice": "mac"`, `"lookup": "no"`): file the app-data request ("Get the app data" below), search nothing, and go to step 6 once you have it, suggesting from its patterns and what they tell you.
- Only the web (`"choice": "web"`, `"lookup": "yes"`): go to step 3. Do not ask for app data at any point in this flow, and do not offer it.
- Neither, they would rather tell you themselves (`"choice": "self"`, `"lookup": "no"`): when they have not already started, ask them to go ahead in one short line. Search nothing and do not ask for app data. What they then tell you about themselves is saved as in "What the user tells you" below.
- Anything else (describing what annoys them, a question): no search and no app data. Answer them and use what they told you; write `consent.json` only when they clearly declined both.

### Get the app data

Follow the `suggest-apps` skill's steps 1 and 2: read what you already hold, and ask only for the rest (never `messages`, `shell-history` or `code-projects`). In this workspace's first chat, when `data/.skills/read-app-data/first_chat_offer.json` does not exist, file the request with `"origin": "first-chat-offer"` and write that file, as the `read-app-data` skill's "The first-chat offer" says (this is that offer). The reason: "To suggest apps that fit how you work, and to help find you on the web if you asked for that, I'd like to see which apps you have installed and the sites you visit most." (Leave out the part about the web for `"choice": "mac"`.) Do not wait for its answer before going on with anything else; when the answer comes, read what was granted for patterns (`suggest-apps` step 3). When nothing is granted, say so in one line and go on without it.

## 3. Get their name

Read the name in the same turn as the user's yes, with no question and no approval before it (when you already read it to decide whether to offer, use that answer): the offer they accepted names "the name on your account", so read it straight away from Imbue Studio:

```bash
latchkey curl -sS "http://latchkey-self.invalid/minds-api-proxy/api/v1/agents/$MNGR_AGENT_ID/owner-name"
```

The answer is `{"display_name": "<name>", "email_domain": "<domain>", "is_shared_with_others": true | false | null}` for the account that owns this workspace.
`email_domain` is the domain of the owner's email only when it names an organization (`imbue.com`); it is null for personal mail providers such as Gmail, and the address itself is never answered. Keep it for step 5: it ranks the results, and it is never searched with unless the user says yes to the question there.
Ask the user to type their name instead when `display_name` is null or the call fails (for example when Imbue Studio is not running on their computer), and end your turn.
Never take a name from anywhere else, such as a file, a commit author, or an email address, and never ask for the accounts permission for this: that route is all the lookup needs.

### Check who can see the workspace

Sharing is on by default, only so the owner can reach the workspace from their own phone or browser; that admits nobody else, and is no reason to ask.
What matters is whether other people (or a visiting agent) can get in, which the `owner-name` answer's `is_shared_with_others` says (you cannot read the workspace's grants from inside it).

- `consent.json` already has `"only_me": true` or `"shared_ok": true`: go straight to step 4, with no question.
- **`is_shared_with_others` is `false`**: go straight to step 4, with no question.
- **`is_shared_with_others` is `true`**: send exactly this message, word for word, then end your turn:

> This workspace is shared with other people, so they would also be able to see what I save about you, on **What Studio knows about me**. Do you still want me to go ahead, or should I skip the search?

- **`is_shared_with_others` is `null`** (Imbue Studio could not read who has access): when sharing is off (the `imbue-studio` skill's "Shared workspaces are untrusted" section says how to tell), go straight to step 4; when it is on, send exactly this message, word for word, then end your turn:

> I couldn't check whether anyone else can get into this workspace. If they can, they would also see what I save about you, so I need to check with you first. Can anyone besides you use this workspace? And if so, do you still want me to go ahead?

- A clear "only me": add `"only_me": true` to `consent.json` and go to step 4. Do not ask again in later chats.
- A clear "others can use it, and search anyway": add `"shared_ok": true` to `consent.json` and go to step 4. Do not ask again in later chats. In step 6, also say that people who can use this workspace can see the **What Studio knows about me** page.
- "Skip it", "not sure", or anything that is not a clear go-ahead (a bare "others can" is not one): set `consent.json`'s `lookup` to `"no"`, do not search, and go to step 6 with what they tell you about themselves.

## 4. Search

Search through Imbue Studio's web lookup, which passes each request to [treg](https://treg.to/), a service that routes it to one of many public web search engines.
Imbue holds the treg account and pays for it: there is nothing for the user to connect, sign up for, or pay, and no key ever comes into this workspace.
The lookup only offers treg's web search and page-reading tools; nothing else in treg's catalog can be reached through it (see "Why only a web search" below).
Never use your own web search or web fetch tools for this lookup: the user agreed to this one.

A search (the answer is `{"results": [{"title", "url", "snippet"}], "served_by"}`):

```bash
latchkey curl -sS -X POST \
  "http://latchkey-self.invalid/minds-api-proxy/api/v1/agents/$MNGR_AGENT_ID/web-lookup/search" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg q "<the query>" '{query: $q, limit: 10}')"
```

Reading one page (the answer is `{"pages": [{"url", "markdown"}], "served_by"}`):

```bash
latchkey curl -sS -X POST \
  "http://latchkey-self.invalid/minds-api-proxy/api/v1/agents/$MNGR_AGENT_ID/web-lookup/extract" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg u "<the url>" '{url: $u}')"
```

When a call answers an error, do not search with anything else:

- `409` with code `no_signed_in_account`: the workspace's account is signed out of Imbue Studio. Ask the user to sign in, then try again.
- `429` (`web_lookup_rate_limited`): the account's daily lookups are used up. Say so, and go to step 6 with what they tell you about themselves.
- `403` (`network_restricted`), `502` or `503`: the lookup is not available right now. Say so, and go to step 6 the same way.

Go straight from the name to the results, and keep it quick: the user is waiting on this turn.

- **Search in parallel.** Run every search call in one batch of tool calls rather than one after another: the name alone, and, when the user gave you a detail (a city, an employer, a field), the name with that detail. At most 3 searches.
- **Only the name and what the user told you.** A search may contain the name and details the user stated in this conversation, nothing else. Never add anything you know from elsewhere: not their email address, not the workspace's name, files, git history or connected accounts, and not anything from earlier memories. There are two exceptions, each only after the user says yes to it: the organization behind `email_domain` (step 5), and a detail from their app data they pick in "Search details from their Mac" (step 5). Searching with anything else looks the user up with information they never offered.
- **Widen a thin first batch.** If the first batch shows one person or none, run one more batch at once, before asking anything: the name with `LinkedIn`, with `GitHub`, and with `personal website` (at most 3 more searches). Most names belong to several people, and a single candidate is more often a thin result than a match.
- **Show options from the search results alone.** Do not read any page before the user confirms: titles, snippets and links are enough to tell the people apart and to ask which is them. Pages are read in step 5, only for the person they confirm.

**Sign-ins.** Some results, LinkedIn profiles most often, show only part of a page until someone signs in.
Use such a result only as far as its search result (title and snippet) shows, and do not try to read the page itself: it would answer a sign-in wall, not the profile.
Never sign in to anything, and never use the user's own connected accounts (their LinkedIn included) to see more.
If that profile matters, say so when you show the results in step 5, and offer the user's own way to share it: paste the parts of it they are happy to share, or upload the PDF LinkedIn can make of their own profile (**Save to PDF**, in the menu on their profile page).
What they share themselves goes in `sources.json` with `"origin": "user"` and a null `url`, and its facts end with `(you told me)` in the memory.

Read only pages that are public: do not look for contact details, addresses, family, finances, or health, even when a page offers them.

Save every result you use to `sources.json` as you go, and every page you later read: a JSON array of `{"url", "title", "fetched_at": "YYYY-MM-DD", "origin": "search" | "page" | "user", "served_by"}` (`served_by` is the engine the lookup names).

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

**Typed answers.** Ask in plain text, and when the message has more than one question, number them so the user can answer all of them in one typed reply. For example:

> 1. Is one of these you? Tell me the number (or numbers, if two are the same person), or "none".
> 2. Your account email is at mit.edu. Want me to also search for your name with MIT?
> 3. From your Mac, these might help find you: MIT (your browser history), machine learning (your installed apps). Want me to search with any of them?

Read the whole reply and act on every answer in it. A question they leave unanswered counts as no. When they name a person, you are done searching, whatever else they said; otherwise run every search they said yes to, in one batch, and show the people again the same way, without the searches already used or turned down.

**Rank by the email's organization.** When `email_domain` is set, list first any person whose title, snippet or link mentions that organization (its domain, or its name: `Imbue` for `imbue.com`), and say why in that line: "mentions Imbue, your email's organization". This only orders what the search already found; it sends nothing new. When no result mentions it, do not remark on that; the question below is the only place the email's organization comes up.

**Offer to search with it.** When `email_domain` is set, no result mentions that organization (whether the name searches, the widened batch included, found other people or no one at all), and `consent.json` has no `"email_org"` answer yet: show the people you found as usual (or say you found no one by that name), and ask, as one of the message's questions, with the organization's readable name in place of `<Org>` and its domain in place of `<domain>`: "Your account email is at <domain>. Want me to also search for your name with <Org>?"

- A yes: set `"email_org": "yes"` in `consent.json`, run one search for the name with `<Org>` (never the address), and show the people again, ranked as above.
- A no, or no answer to it: set `"email_org": "no"` in `consent.json` and never search with it.

### Search details from their Mac

Only for `"choice": "both"`. The name search and the app-data request run at the same time (step 2), so either can finish first.

1. When you show the results, and they list more than one person or none: if the app data has been read by then, ask about its details as one of that message's questions. If its answer comes later and they have not replied yet, send one short message with that question alone. When the results show one clear person, do not propose any.
2. Pick at most 3 short details from the patterns that a public page about them would also mention: an organization or school they use often (`MIT` from many visits to mit.edu), their field (`machine learning`), a public project or handle. Never a person's name, an address, anything about health, money or relationships, and never one listed in `app_terms.declined`.
3. Name each with where it came from, and nothing about the raw data behind it: "From your Mac, these might help find you: MIT (your browser history), machine learning (your installed apps). Want me to search with any of them?" No buttons.

   Nothing from the app data goes into a search until they say yes to it, even though they chose both.
4. Each detail they say yes to goes in `app_terms.approved`; search for the name with each (one search per detail, in one batch), and show the people again. Every detail they did not say yes to goes in `app_terms.declined`: never propose those again.
5. A confirmed fact that came from one of these searches cites its web page as usual; a detail they picked that no page confirms, kept as a fact, ends with `(you told me)`, since they chose it.

Raw app data (page titles, URLs, file names, note or event text) never goes into a search, the chat, or a memory.

End your turn and wait.
Keep only what they confirm.

Once they confirm one, read up to 5 of that person's pages from the results through the lookup's page read, all in one batch of tool calls, to fill in what they do and care about; skip any page behind a sign-in (see "Sign-ins" above).
When none is them, either search once more with the detail they give, or drop the lookup and go to step 6 with what they have told you.

Write `user-public-profile.md` in your memory directory from the confirmed person only, in the memory format `CLAUDE.md` sets up (frontmatter with `name: user-public-profile`, a one-line `description`, `type: user`).
The body is one fact per line, in exactly this shape, so the "What Studio knows about me" page can show and delete each one:

```markdown
- <fact> ([<source title>](<url>)) <!-- found YYYY-MM-DD -->
- <fact the user shared themselves> (you told me) <!-- found YYYY-MM-DD -->
```

A few lines on what they do and care about, then a last plain line (not starting with `- `) saying the facts came from a web lookup the user asked for on that date.
The file may already exist from an earlier lookup: keep every line already in it, add yours after its last fact, and do not duplicate one.
Add its pointer to `MEMORY.md`, unless it already has one.
Keep every fact from the lookup in this one memory, not spread across others, so forgetting it is complete.

Delete the unconfirmed entries from `sources.json`.

## What the user tells you

What the user tells you about themselves (what they do or study, where they are, what they care about, what keeps annoying or overwhelming them), whether after **I’ll tell you myself**, while answering what annoys them, or anywhere else in a chat, is saved the way your memory instructions say: as ordinary `user` memories in your memory directory, one fact per file, each with its line in `MEMORY.md`.
The **What Studio knows about me** page shows every memory in that directory, grouped by type, and lets the user edit and delete each one, so it needs no other copy: do not also add these facts to `user-public-profile.md`, which holds only what the lookup found.

- Save facts about the person, not details of what they are building or asking you to do ("wants a grading app" is not one; "grades 20 problem sets a week" is).
- When they correct a fact, update that memory rather than adding another.
- Every time you save or change a memory about the user, say so at the end of that reply, naming the page and linking it (its `link` from `uv run --no-sync workspace-layout list`), with exactly this sentence:

  > I’ve added what you told me about yourself to [What Studio knows about me](<link>), where you can edit or delete it.

  (When you changed a memory rather than adding one, say "updated" in place of "added".) Never save a memory about the user without that sentence in the same reply.


## 6. Suggest

When they chose `both` or `mac` and the app-data answer has not come yet, wait for it (end your turn, saying in one line that you will suggest once they have answered the request). Then suggest from what they confirmed or told you and the patterns in their app data (`suggest-apps` step 3; its rules on what never to name apply). For `web` and `self`, never use app data, even if this workspace holds some.

Suggest three to five things to build, each one or two sentences, each tied to something about them ("You publish a newsletter, so: a page that drafts each issue from your saved links").
Prefer things that can be built in this workspace in one sitting.
Ask which one they want, and take it from there with the skill that fits the choice.

Do not recite what you learned about them back at length; the ideas should show it.
Mention once, in passing, that you will remember this for future chats, and link the **What Studio knows about me** page (its `link` from `uv run --no-sync workspace-layout list`), where they can see everything you remember about them, each lookup fact with its source, and add to, edit or delete any of it.

## Deleting

When the user asks you to forget what you found, delete `user-public-profile.md`, its line in `MEMORY.md`, and `sources.json`, as the page's "Forget what the lookup found" does, then check your other memories for any fact that came from the lookup (one citing a link in `sources.json`) and remove it too. Memories of what they told you stay.
When they ask you to forget everything about them, also delete every `user` memory and its line in `MEMORY.md`; the page's "Forget everything" deletes every memory of every type.
Say what you deleted.
When they say not to look them up again, also set `consent.json` to `"no"`.
