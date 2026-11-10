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
4. **Search.** Through Imbue Studio's web lookup (`POST /api/v1/agents/<id>/web-lookup/search` and `/extract`, reached through latchkey's `minds-api-proxy`), which forwards to the connector with the owner's signed-in session; the connector calls [treg](https://treg.to/) with an Imbue-owned team token held server-side, so the user supplies and pays for nothing and no key enters the workspace. The connector calls only treg's `treg.web.search` and `treg.web.extract`, caps each call's cost at $0.05, and limits each account to 30 searches and 60 page reads a day. All searches go in one parallel batch (the name alone, plus the name with any detail the user volunteered; at most 3), a second batch (the name with LinkedIn, GitHub and personal website; at most 3 more) when the first shows one person or none, and no page is read before the user confirms: the options are built from titles, snippets and links. After the user confirms, up to 5 of that person's pages are read in one parallel batch. Public pages only. No contact details, addresses, family, finances or health, and nothing behind a sign-in: a LinkedIn profile, for example, is used only as far as its search result shows; for more, the user can paste their profile or share LinkedIn's own PDF export of it. When the lookup is unavailable, there is no search with anything else. Searches contain only the name and details the user states in the conversation, never the email address, workspace files, connected accounts or earlier memories. The organization behind the account email (its domain alone, which Imbue Studio answers only for organizations, never for personal providers such as Gmail) ranks results that mention it first, and, only when the name searches find no one at all, is searched with after the user says yes to a one-time question.
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
| Data protection (information gathered from other sources, e.g. GDPR Art. 14) | The user sees everything gathered, at the time it is gathered, with its source, and controls its deletion. It is kept only in their workspace; the name and pages read pass through Imbue's connector on the way to the search service, which stores neither. |
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
- A workspace with sharing on is asked about once, with a fixed question and two reply buttons ("Only me, go ahead" / "Skip the search"), because sharing is often only the owner's own phone or browser. "Only me" is saved in `consent.json` and not asked again; anything else counts as a "no" and nothing is searched.
- Backend: the connector's web lookup routes (mngr-internal `apps/remote_service_connector`), the desktop routes that reach them, and the owner-name route. The name and the pages read pass through Imbue's connector on their way to treg; the connector records the account, time, kind of call, workspace and answering engine for its daily limits, never the query or the URL. The one outside service is treg, under Imbue's account and billing; the offer says the search goes through an outside search service.

## Seeing and controlling what is remembered

Two additions make the promise that the user can see and delete everything visible rather than something they have to ask for. Neither collects anything new.

### Refresh on request (planned)

When the user asks to update what the agent knows ("update what you know about me", "look me up again"), the agent reruns steps 3 to 5 for the person already confirmed: it reads the owner's name, searches the same way, and shows only what is new or changed since `user-public-profile.md` was written, each with its link, for the user to confirm or drop.
Confirmed changes are written into the same memory, with the date of the refresh in its last line; dropped ones are deleted from `sources.json`.
Their earlier yes stands (`consent.json` says `"yes"`), so there is no new offer; a `"no"` there means no refresh until they say yes again.
A refresh never runs on its own: no schedule, no routine, unless the user explicitly sets one up with the `manage-routines` skill and is told it repeats the lookup.
This is a change to the skill's instructions only.

### A "What Studio knows about me" page

A small workspace app (`system/apps/about_me/`) that shows the lookup's memory as a page:

- Each remembered fact, with the link it came from and the date it was found or confirmed; facts the user shared themselves are marked as such.
- A delete control on each fact, which removes its line from `user-public-profile.md` and its entry from `sources.json` when no remaining fact cites it.
- "Forget all of this", which does what asking the agent to forget does: deletes `user-public-profile.md`, its line in `MEMORY.md`, and `sources.json`.
- An empty state when nothing is remembered, saying how to ask for a lookup.

It reads and writes only those three files; it never searches or reads the web.
For the page to read the memory reliably, the skill writes `user-public-profile.md` with one fact per line in a fixed shape, `- <fact> ([<source title>](<url>)) <!-- found <YYYY-MM-DD> -->`, and the page treats any line it cannot parse as one fact it can still show and delete whole.
The skill links the page once, in step 6, where it mentions that it will remember this.

Open for this page: whether it belongs in the workspace or in Imbue Studio's own settings once memories from other sources exist.

## Open questions

- Legal review of the offer's wording before release.

## Testing

The skill is instructions only, so it has no unit tests; the app's welcome has unit tests in mngr-internal (`creationTranscript.test.ts`, `welcomeChat.test.ts`, `CreatingPage.test.ts`).
Check the whole flow by hand, in a workspace created on this computer from a dev build of Imbue Studio (so the workspace gets this branch's skill), signed in to an account whose display name is set.

1. **Welcome.** The first chat's welcome is "You're all set." over "The best way to start is to build something. Do you have any ideas?" with **Yes** and **No** buttons, and nothing else. **Yes** gets a question about your idea and no offer. **No** gets the fixed offer, word for word as in step 2 of the skill. With an account that has no display name, **No** gets only the question about what annoys you.
2. **Search for me.** Click **Search for me**. In the same turn, with no permission prompt and without making the offer again, the agent reads your account name and searches.
3. **Name.** The search uses the name on your account.
4. **Sharing.** With sharing turned on for the workspace, the agent asks once whether anyone besides you can use it, with **Only me, go ahead** and **Skip the search** buttons, before any search. **Only me** searches and records `"only_me": true` in `consent.json`, and a later chat does not ask again; **Skip the search** searches nothing and moves on to asking about you. With sharing off, there is no question.
5. **Search.** The agent searches through Imbue Studio's web lookup, and its searches go out together (at most 3, plus at most 3 more together when the first ones find one person or none); it reads no page yet, and it saves the results it uses to `data/.skills/personalized-ideas/sources.json` with URLs and dates. It does not sign in to anything; a LinkedIn result is shown from its search snippet only.
6. **Confirm.** In that same turn the agent shows the distinct people it found, one line and one link each, asks which is you, and ends its turn. After you pick one, it reads up to 5 of that person's pages together. Answer with one of them (another run: answer "none of these", and confirm it narrows the search once or drops it).
7. **Organization.** Sign in with an account whose email is at an organization. A result that mentions that organization is listed first, with a note saying why. Only when the name searches find no one at all, the agent asks once whether to also search with the organization's name, with **Yes, include <Org>** and **No, don't** buttons: yes runs one search with the organization's name (never the email address) and shows the people again; no searches nothing more. Either answer is recorded as `email_org` in `consent.json` and not asked again. With a Gmail or other personal address, nothing is ranked and nothing is asked.
8. **Remember.** `data/memories/user-public-profile.md` exists, each fact followed by its link, ending with a line saying it came from a lookup you asked for; `MEMORY.md` has its pointer; `sources.json` holds only the confirmed person's entries; `consent.json` says `"yes"`.
9. **Ideas.** Three to five ideas, each tied to something about you, and one passing mention that it will remember this, linking the What Studio knows about me page.
10. **Later chats.** A new chat knows the facts without being told.
11. **Forget.** Say "forget what you found". The memory file, its `MEMORY.md` line and `sources.json` are gone, and the agent says what it deleted.
12. **No offer.** In fresh chats, none of these get an offer or a lookup: "I want to build a budget tracker"; "I have an idea"; **Yes** on the welcome; describing an idea instead of clicking a button; a message prefixed `[from: Someone]`; any message after answering **I’ll tell you myself** (which is saved in `consent.json`).
