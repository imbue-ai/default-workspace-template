# Personalized ideas: an opt-in web lookup by name

## Purpose

A user who does not know what to build gets generic suggestions today.
This adds the `personalized-ideas` skill: the workspace agent offers to look the user up on the public web by name, and, only after an explicit yes, searches, has the user confirm which results are them, and suggests a few things to build that fit them.

It is the search step of the project that began with retrieving the user's name at Google sign-in (mngr-internal `specs/google-profile-name/spec.md`).
That spec lists what the search must settle before it ships; this spec answers each point below.

## Flow

1. **Trigger.** The user does not know what to build or asks for ideas. The skill's description is what routes the agent to it. Imbue Studio's welcome, the message a new workspace's first chat closes on (mngr-internal `apps/minds/frontend/src/models/creationTranscript.ts`, `WELCOME_BODY`), asks "The best way to start is to build something. Do you have any ideas?" with **Yes** and **No** reply buttons; **No** is that trigger. The lookup is never offered once the user has described something to build or said they have an idea; the agent helps with that instead, until they say they are stuck.
2. **Offer.** After a no, or when the user asks for ideas later, the agent reads whether the account has a display name (the owner-name route, below; reading it searches nothing). With one, it sends one fixed message, word for word: what is annoying, overwhelming, or tedious in their digital life, and, as the alternative, a web lookup by the name on their account, with **Search for me** and **I’ll tell you myself** buttons. Without one, it asks only the first question. One wording everywhere means the offer is reviewed once and is never improvised or personalized, so it cannot read as though the agent already knows something about the user. It says what is searched (their name, on public pages), why (to suggest things to build), that they confirm what is really them, and that what they confirm is remembered for future chats, in their workspace, until they ask it to forget. Only a clear yes to the search starts the lookup. The answer is saved in `data/.skills/personalized-ideas/consent.json`, so a "no" is not asked again.
3. **Name.** In the same turn as the yes, the agent reads the workspace owner's display name from `GET /api/v1/agents/<id>/owner-name` (mngr-internal `apps/minds`), which answers that name and, when the account email belongs to an organization, that email's domain, and nothing else about the account, with no permission prompt: the accepted offer names "the name on your account". When there is no name, or the call fails, it asks the user to type their name.
4. **Search.** The agent's own web search tool, all searches in one parallel batch (the name alone, plus the name with any detail the user volunteered; at most 3), a second batch (the name with LinkedIn, GitHub and personal website; at most 3 more) when the first shows one person or none, and no page read before the user confirms: the options are built from titles, snippets and links. After the user confirms, up to 5 of that person's pages are read in one parallel batch. Public pages only. No contact details, addresses, family, finances or health, and nothing behind a sign-in: a LinkedIn profile, for example, is used only as far as its search result shows; for more, the user can paste their profile or share LinkedIn's own PDF export of it. (A later change moves the search to an Imbue-held treg account; see mngr-internal branch `mngr/web-lookup-proxy`.) Searches contain only the name and details the user states in the conversation, never the email address, workspace files, connected accounts or earlier memories. The organization behind the account email (its domain alone, which Imbue Studio answers only for organizations, never for personal providers such as Gmail) ranks results that mention it first, and, when no result mentions it, is searched with after the user says yes to a one-time question.
5. **Confirm.** The agent groups the results into the distinct people they describe and asks which, if any, is the user. Only the confirmed person is kept.
6. **Suggest.** Three to five ideas, each tied to something about the user.

What is kept:

- `user-public-profile.md`, a `user` memory in the agent's built-in memory (`data/memories/`, indexed in `MEMORY.md`): a short summary of the confirmed person, each fact linked to its source. Because it is in memory, every later chat knows it, not only this skill.
- `sources.json` under `data/.skills/personalized-ideas/`: the raw results used.

All facts from the lookup go in that one memory, so forgetting is complete. Asking the agent to forget deletes the memory, its `MEMORY.md` line, `sources.json`, and any other memory citing a source from the lookup.

## The constraints from the name spec

| Constraint | How this answers it |
|---|---|
| Google user data policy: tell users how their Google data is used, and get consent for a new use | The name is used only after the user says yes to a lookup described in plain words that names "the name on your account". Nothing is searched without that yes. |
| What users are told | The offer states what is searched, that it goes through an outside search service, why, that it is remembered for future chats, where it is kept, and that it can be deleted. |
| Data protection (information gathered from other sources, e.g. GDPR Art. 14) | The user sees everything gathered, at the time it is gathered, with its source, and controls its deletion. It is kept only in their workspace and is never sent to Imbue's servers. |
| Imbue's commitments ("honest software") | No lookup without asking; no silent profile. |
| Wrong person | Results are grouped by person and the user picks; unconfirmed results are discarded. |

## Why web search only, not person enrichment

People-search and sales-lead databases (Lusha, People Data Labs, Apollo, Hunter and others, some reachable through tool brokers such as treg) return work emails, phone numbers, social profiles and employment history for a name.
The skill does not use them:

- They would make the lookup seem creepy. The offer promises a look at public web pages; showing someone their own phone number or email history, from a database they never gave it to, is the opposite of what they agreed to.
- They are often wrong: a found email or phone is the provider's best match, not a confirmed one.
- They return exactly the data this spec excludes, and gathering it about a person from brokers carries notice duties (for example GDPR Article 14) that a public search, shown to the user as it happens, does not.

## Exclusions

- A message from someone other than the owner (`[from: <name>]`): the name belongs to the owner. The agent suggests general ideas instead.
- Sharing is on by default only so the owner can reach the workspace from their phone; that admits nobody else and asks nothing. The owner-name route answers `is_shared_with_others` (any grant, on the workspace or one of its apps, admitting someone besides the owner, or a visiting agent holding the whole workspace; never who), and only when it is true does the agent ask, once, with a fixed question and two buttons ("Others can, but go ahead" / "Skip the search"). When Imbue Studio cannot read it, a published workspace gets the three-button question ("Only me, go ahead" / "Others can, but go ahead" / "Skip the search"). Either go-ahead is saved in `consent.json` (`only_me` or `shared_ok`) and not asked again; anything else counts as a "no" and nothing is searched. Only the owner's own messages can start the search, and it is always for the owner's account name.
- Backend: one Imbue Studio route, `GET /api/v1/agents/<id>/owner-name` (the owner's display name, their email's organization domain, and whether anyone besides them can get in); nothing in the connector changes. The search is the agent's own web search; the offer says it goes through an outside search service.

## Planned: seeing and controlling what is remembered

Two additions make the promise that the user can see and delete everything visible rather than something they have to ask for. Neither collects anything new.

### Refresh on request

When the user asks to update what the agent knows ("update what you know about me", "look me up again"), the agent reruns steps 3 to 5 for the person already confirmed: it reads the owner's name, searches the same way, and shows only what is new or changed since `user-public-profile.md` was written, each with its link, for the user to confirm or drop.
Confirmed changes are written into the same memory, with the date of the refresh in its last line; dropped ones are deleted from `sources.json`.
Their earlier yes stands (`consent.json` says `"yes"`), so there is no new offer; a `"no"` there means no refresh until they say yes again.
A refresh never runs on its own: no schedule, no routine, unless the user explicitly sets one up with the `manage-routines` skill and is told it repeats the lookup.
This is a change to the skill's instructions only.

### A "What Studio knows about me" page

A small workspace app (`system/apps/about_me/`) that is the user's view onto the agent's own memory: Claude's built-in memory in `data/memories/`, one memory per file, indexed by `MEMORY.md`. It adds no store of its own, so what the page shows is exactly what the agent reads at the start of every chat, and a change on the page is a change to what the agent knows.

- Every memory, grouped by its type: **About you** (`user`), **How you like to work** (`feedback`), **Your projects** (`project`), **Links and references** (`reference`), and **Other notes** for a file with no known type. Each shows its title (its `MEMORY.md` line's), its text, and **Edit** and **Delete**.
- **Edit** rewrites only the memory's description, its body, and its `MEMORY.md` line's hook; every other frontmatter line stays. **Delete** removes the file and its `MEMORY.md` line.
- The web lookup's memory, `user-public-profile.md`, under **About you** fact by fact: each with the link it came from and the date, its own **Edit** (words only; the link and date stay) and **Delete** (the line, and the sources no remaining fact cites), and **Forget what the lookup found** (the file, its `MEMORY.md` line, and `sources.json`).
- **Add something about yourself**, which writes a new `user` memory in the format the agent writes its own, with its `MEMORY.md` line.
- **Forget everything**, after a confirm, which deletes every memory, `MEMORY.md`, and `sources.json`.
- An empty state when nothing is remembered, saying the user can add something or tell the agent about themselves.

What the user tells the agent about themselves in chat (after "I’ll tell you myself", or anywhere else) is saved as ordinary `user` memories, as the agent's memory instructions say, and so shows on the page with no second copy. Whenever it saves or changes one, the agent ends that reply with a fixed sentence naming and linking the page: "I’ve added what you told me about yourself to What Studio knows about me, where you can edit or delete it."

It reads and writes only the memory directory and `sources.json`; it never searches or reads the web. A change names the file's current content by a hash, so a page left open while the agent rewrote a memory reloads instead of overwriting it.
For the page to show the lookup fact by fact, the skill writes `user-public-profile.md` with one fact per line in a fixed shape, `- <fact> ([<source title>](<url>)) <!-- found <YYYY-MM-DD> -->`, and the page treats any line it cannot parse as one fact it can still show and delete whole.
The skill links the page in step 6, and whenever it saves a memory about the user.

Open for this page: whether it belongs in the workspace or in Imbue Studio's own settings once memories from other sources exist.

## Open questions

- Legal review of the offer's wording before release.

## Testing

The skill is instructions only, so it has no unit tests; the app's welcome has unit tests in mngr-internal (`creationTranscript.test.ts`, `welcomeChat.test.ts`, `CreatingPage.test.ts`).
Check the whole flow by hand, in a workspace created on this computer from a dev build of Imbue Studio (so the workspace gets this branch's skill), signed in to an account whose display name is set.

1. **Welcome.** The first chat's welcome is "You're all set." over "The best way to start is to build something. Do you have any ideas?" with **Yes** and **No** buttons, and nothing else. **Yes** gets a question about your idea and no offer. **No** gets the fixed offer, word for word as in step 2 of the skill. With an account that has no display name, **No** gets only the question about what annoys you.
2. **Search for me.** Click **Search for me**. In the same turn, with no permission prompt and without making the offer again, the agent reads your account name and searches.
3. **Name.** The search uses the name on your account.
4. **Sharing.** With sharing on only for your own phone (nobody else added), the search starts with no question. Add someone to the workspace's sharing, then start the lookup in a new workspace or chat: before searching, the agent says the workspace is shared with other people and asks, with **Others can, but go ahead** and **Skip the search**. Going ahead records `"shared_ok": true` in `consent.json`, and a later chat does not ask again.
5. **Search.** The agent searches with its own web search tool, and its searches go out together (at most 3, plus at most 3 more together when the first ones find one person or none); it reads no page yet, and it saves the results it uses to `data/.skills/personalized-ideas/sources.json` with URLs and dates. It does not sign in to anything; a LinkedIn result is shown from its search snippet only.
6. **Confirm.** In that same turn the agent shows the distinct people it found, one line and one link each, asks which is you, and ends its turn. After you pick one, it reads up to 5 of that person's pages together. Answer with one of them (another run: answer "none of these", and confirm it narrows the search once or drops it).
7. **Organization.** Sign in with an account whose email is at an organization. A result that mentions that organization is listed first, with a note saying why. When no result mentions it, the agent asks once whether to also search with the organization's name, with **Yes, include <Org>** and **No, don't** buttons: yes runs one search with the organization's name (never the email address) and shows the people again; no searches nothing more. Either answer is recorded as `email_org` in `consent.json` and not asked again. With a Gmail or other personal address, nothing is ranked and nothing is asked.
8. **Remember.** `data/memories/user-public-profile.md` exists, each fact followed by its link, ending with a line saying it came from a lookup you asked for; `MEMORY.md` has its pointer; `sources.json` holds only the confirmed person's entries; `consent.json` says `"yes"`.
9. **Ideas.** Three to five ideas, each tied to something about you, and one passing mention of where the memory is.
10. **Later chats.** A new chat knows the facts without being told.
11. **Add on the page.** On the **What Studio knows about me** page, type something about yourself and click **Add**: it appears under **About you**, and a new chat knows it. Click **Edit** on any memory, change its text and click **Save**: a new chat knows the change. **Delete** removes it. On a lookup fact, **Edit** changes only its words; its link and date stay.
12. **Told in chat.** In a fresh workspace, click **No**, then **I’ll tell you myself**, and describe your job and what annoys you. The reply ends with "I’ve added what you told me about yourself to What Studio knows about me, where you can edit or delete it.", linking the page; the page shows each fact about you under **About you**, and nothing about what you want to build.
13. **Forget.** Say "forget what you found": the lookup's facts and `sources.json` are gone, what you told the agent stays, and the agent says what it deleted. Then click **Forget everything** on the page and confirm: every memory and `MEMORY.md` are gone, and a new chat remembers nothing about you.
14. **No offer.** In fresh chats, none of these get an offer or a lookup: "I want to build a budget tracker"; "I have an idea"; **Yes** on the welcome; describing an idea instead of clicking a button; a message prefixed `[from: Someone]`; any message after answering **I’ll tell you myself** (which is saved in `consent.json`).
