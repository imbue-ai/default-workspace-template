# Personalized ideas: an opt-in web lookup by name

## Purpose

A user who does not know what to build gets generic suggestions today.
This adds the `personalized-ideas` skill: the workspace agent offers to look the user up on the public web by name, and, only after an explicit yes, searches, has the user confirm which results are them, and suggests a few things to build that fit them.

It is the search step of the project that began with retrieving the user's name at Google sign-in (mngr-internal `specs/google-profile-name/spec.md`).
That spec lists what the search must settle before it ships; this spec answers each point below.

## Flow

1. **Trigger.** The user does not know what to build or asks for ideas. The skill's description is what routes the agent to it. The lookup is never offered, and the welcome's offer never taken up, once the user has described something to build or said they have an idea; the agent helps with that instead, until they say they are stuck.
2. **Offer.** When the account has a display name, Imbue Studio's start flow makes the offer itself, in the welcome the workspace's first chat closes on (mngr-internal `apps/minds/frontend/src/models/creationTranscript.ts`, `LOOKUP_OFFER`); the name is not in the welcome, only the fact that one is known. The agent receives that conversation with the user's first message: a clear yes to it is the yes below, a clear no is recorded, and any other reply records nothing. Otherwise the agent asks once, with the same fixed message, word for word. One wording everywhere means the offer is reviewed once and is never improvised or personalized, so it cannot read as though the agent already knows something about the user. It says what is searched (their name, on public pages), why (to suggest things to build), that they confirm what is really them, and that what they confirm is remembered for future chats, in their workspace, until they ask it to forget. Only a clear yes starts the lookup. The answer is saved in `data/.skills/personalized-ideas/consent.json`, so a "no" is not asked again.
3. **Name.** The agent files an `accounts` permission request (the existing must-ask `minds-accounts-read` grant) and reads `display_name` from `GET /api/v1/accounts`. When the grant is denied, no name is set, more than one account is signed in, or the request cannot be filed, it asks the user to type their name.
4. **Search.** The agent's own web search and web fetch tools, at most 3 searches and 5 page reads, name only (plus any detail the user volunteers), public pages only. No contact details, addresses, family, finances or health, and nothing behind a sign-in: a LinkedIn profile, for example, is used only as far as its search result shows; for more, the user can paste their profile or share LinkedIn's own PDF export of it. (A later change moves the search to an Imbue-held treg account; see mngr-internal branch `mngr/web-lookup-proxy`.)
5. **Confirm.** The agent groups the results into the distinct people they describe and asks which, if any, is the user. Only the confirmed person is kept.
6. **Suggest.** Three to five ideas, each tied to something about the user.

What is kept:

- `user-public-profile.md`, a `user` memory in the agent's built-in memory (`data/memories/`, indexed in `MEMORY.md`): a short summary of the confirmed person, each fact linked to its source. Because it is in memory, every later chat knows it, not only this skill.
- `sources.json` under `data/.skills/personalized-ideas/`: the raw results used.

All facts from the lookup go in that one memory, so forgetting is complete. Asking the agent to forget deletes the memory, its `MEMORY.md` line, `sources.json`, and any other memory citing a source from the lookup.

## The constraints from the name spec

| Constraint | How this answers it |
|---|---|
| Google user data policy: tell users how their Google data is used, and get consent for a new use | The name is read only after the user says yes to a lookup described in plain words, and again approves the accounts permission. Nothing runs without both. |
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

- A message from someone other than the owner (`[from: <name>]`), and any shared workspace: the name belongs to the owner, and what is saved would be visible to others. The agent suggests general ideas instead.
- No backend component: nothing in Imbue Studio's backend or the connector changes. The search is the agent's own web search; the offer says it goes through an outside search service.

## Open questions

- Legal review of the offer's wording before release.

## Testing

The skill is instructions only, so it has no unit tests.
Check it by hand in a workspace: ask "I don't know what to build"; confirm the offer appears and that saying no stops it (and is remembered in a new chat); say yes, approve and then deny the accounts request; confirm the confirmation step and the ideas; confirm `user-public-profile.md` and its `MEMORY.md` line exist and a new chat knows the facts; ask it to forget and confirm the memory, its index line and `sources.json` are gone; send the same message prefixed `[from: Someone]` and confirm no offer; say "I want to build a budget tracker" or "I have an idea" (first in a fresh chat, then as the reply to the welcome's offer) and confirm there is no offer and no lookup. Create a workspace under an account with a display name and confirm the first chat's welcome ends on the offer and that answering yes starts at the accounts request; create one under an account without a name and confirm the welcome has no offer.
