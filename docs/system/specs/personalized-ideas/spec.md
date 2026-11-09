# Personalized ideas: an opt-in web lookup by name

## Purpose

A user who does not know what to build gets generic suggestions today.
This adds the `personalized-ideas` skill: the workspace agent offers to look the user up on the public web by name, and, only after an explicit yes, searches, has the user confirm which results are them, and suggests a few things to build that fit them.

It is the search step of the project that began with retrieving the user's name at Google sign-in (mngr-internal `specs/google-profile-name/spec.md`).
That spec lists what the search must settle before it ships; this spec answers each point below.

## Flow

1. **Trigger.** The user does not know what to build or asks for ideas. The skill's description is what routes the agent to it.
2. **Offer.** The agent asks once, saying what it would search (their name, on public pages), why (to suggest things to build), that it will show what it found for them to confirm, and that the note stays in their workspace and can be deleted. Only a clear yes starts the lookup. The answer is saved in `data/.skills/personalized-ideas/consent.json`, so a "no" is not asked again.
3. **Name.** The agent files an `accounts` permission request (the existing must-ask `minds-accounts-read` grant) and reads `display_name` from `GET /api/v1/accounts`. When the grant is denied, no name is set, more than one account is signed in, or the request cannot be filed, it asks the user to type their name.
4. **Search.** The agent's own web search tool, name only (plus any detail the user volunteers), public pages only. No contact details, addresses, family, finances or health.
5. **Confirm.** The agent groups the results into the distinct people they describe and asks which, if any, is the user. Only the confirmed person is kept.
6. **Suggest.** Three to five ideas, each tied to something about the user.

What is kept: `about-me.md` (a short summary, each fact linked to its source) and `sources.json` (the raw results used), both under `data/.skills/personalized-ideas/`. Asking the agent to forget deletes both.

## The constraints from the name spec

| Constraint | How this answers it |
|---|---|
| Google user data policy: tell users how their Google data is used, and get consent for a new use | The name is read only after the user says yes to a lookup described in plain words, and again approves the accounts permission. Nothing runs without both. |
| What users are told | The offer states what is searched, why, what is kept and where, and that it can be deleted. |
| Data protection (information gathered from other sources, e.g. GDPR Art. 14) | The user sees everything gathered, at the time it is gathered, with its source, and controls its deletion. It is kept only in their workspace and is never sent to Imbue's servers. |
| Imbue's commitments ("honest software") | No lookup without asking; no silent profile. |
| Wrong person | Results are grouped by person and the user picks; unconfirmed results are discarded. |

## Exclusions

- A message from someone other than the owner (`[from: <name>]`), and any shared workspace: the name belongs to the owner, and what is saved would be visible to others. The agent suggests general ideas instead.
- No backend component: nothing in Imbue Studio or the connector changes, and no search vendor is added.

## Open questions

- Legal review of the offer's wording before release.
- Whether the start flow should also offer it. This spec keeps it in chat, when the user is unsure what to build.

## Testing

The skill is instructions only, so it has no unit tests.
Check it by hand in a workspace: ask "I don't know what to build"; confirm the offer appears and that saying no stops it (and is remembered in a new chat); say yes, approve and then deny the accounts request; confirm the confirmation step and the ideas; ask it to forget and confirm the files are gone; send the same message prefixed `[from: Someone]` and confirm no offer.
