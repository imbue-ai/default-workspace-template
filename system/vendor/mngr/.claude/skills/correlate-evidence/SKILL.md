---
name: correlate-evidence
description: Join minds evidence across observability stores, and decide what an absence actually means. Owns the correlation keys, the client/server identity gap and the moves that get around it, and the discipline of never trusting a zero before proving the search reaches where the evidence would live. Pure analysis; needs no credentials and runs no commands. Used by investigate-bug locally and by the mngr-seer investigator.
---

# Correlating evidence, and reading absence

You have evidence from one or more stores, and gaps between them.
This step joins what you have and -- more often the decisive part -- establishes what the gaps mean.

It runs nothing.
Where it says a key exists, acquiring the value is the caller's job (`access-sentry`, `access-bugsink`, `access-openobserve`, or a bundle the mngr-seer orchestrator pre-fetched).

## Correlation keys

| Key | Where it lives | Use |
|---|---|---|
| Bugsink `event_id` | `internal_error` response bodies | Direct jump from a client-visible 500 to the server exception |
| Deploy id | Sentry `release` tag, Bugsink `release`, the connector's `/version` | Pin which code was live |
| Environment | Sentry environment (`production`/`staging`/`development`); Bugsink environment (concrete env name); OpenObserve `minds_env` | Keep tiers separate; dev and CI installs report to dev |
| SuperTokens user id (hyphenated UUID) | OpenObserve `http_request.user`, connector DB rows | The server-side identity key |
| `anonymous_user_id` (32-hex) | Sentry `user.id`; `~/.minds*/anonymous_user_id` on the user's machine | The client-side identity key; ties every desktop surface of one install together |
| Email | Manual bug-report bodies only; an operator-gated `minds-admin` account lookup returns the SuperTokens user id | The starting point of most investigations |

## The identity gap

**The two identity spaces never meet server-side.**
The Sentry `anonymous_user_id` has no server-side mapping, and server-side ids never appear in automatic Sentry events.

So you cannot get from an email to a user's automatic Sentry events, nor from a Sentry event to server logs, by identity alone.
This is a property of the systems, not a gap in your search.

The one manual bridge: a cooperating user can read `~/.minds*/anonymous_user_id` off their own machine, which unlocks their automatic Sentry events.

Otherwise, bridge on **time + deploy id + error shape** instead.
That composite is weaker than an identity join and you should say so when you rely on it.

## When the target user is invisible in a store

Two moves recover an investigation that identity cannot carry:

- **Borrow a timestamp.**
  Find another instance of the same failure signature -- a body-level search for the exact error phrase -- then check server-side health at *that* instance's exact window.
  This answers "was the server serving?" without ever locating the target user.
- **Measure blast radius**, with the same phrase search, and read the distribution across flows as a signal.
  A failure that appears only in background retry loops points at transient client-side network state, not at the server.

## Interpreting absence -- before you trust any zero

1. **Run a positive control first.**
   Before concluding "user X / error Y is absent", the same search must be run for a token *known* to exist in that store.
   A zero is only meaningful after the search method has proven it reaches where the evidence would live.
   This is the single most important rule in this skill: an unproven search method makes every subsequent zero worthless, and zeros are what most investigations turn on.
2. **Consent gates automatic events only.**
   The `report_unexpected_errors` setting drops all automatic desktop events in-process (`_AutomaticReportingGate` in `libs/imbue_common/imbue/imbue_common/sentry/core.py`), but manual bug reports always send.
   So: no bug report means the user did not file one -- a real signal.
   No automatic events means consent-off *or* nothing happened -- ambiguous, and must be reported as ambiguous.
3. **Mind the retention windows.**
   Each store ages events out on its own schedule, and Bugsink issue rows outlive their events.
   An absence older than a store's window is not evidence.
4. **Mind the tier.**
   Dev and CI installs report to the `development` Sentry project.
   A production-only search that finds nothing has not searched the place a developer's failure went.
5. **Mind the expected-empty stores.**
   `classify-failure` names which stores cannot hold this failure's evidence at all.
   A zero from one of those is a confirmation of the classification, not a finding.

## When you did not gather the evidence yourself

An agent working from a pre-fetched bundle -- the mngr-seer investigator -- cannot run a positive control, because it cannot run a search.
The rule does not relax; the obligation moves.

For absence to be interpretable at all, whatever performed the acquisition must have recorded, alongside the evidence:

- which stores were queried, and which were not;
- for each query, the filter used and whether it returned zero;
- for each store queried, evidence that the query method worked -- a positive control, or a non-empty result from a sibling query.

Without that record, an air-gapped consumer must treat every gap as **unasked, not empty**, and say so.
Reporting "no server-side evidence" when the truth is "nobody looked server-side" is the failure mode this section exists to prevent.

## Output

State:

1. What joined, and on which key.
2. What did not join, and whether that is the identity gap (structural) or a real absence.
3. For each absence you are relying on: which of the five checks above it survived.
4. Any composite bridge (time + deploy + shape) you leaned on, flagged as weaker than an identity join.

## Related skills

- `classify-failure` -- run first; tells you which stores are expected-empty.
- `access-sentry`, `access-bugsink`, `access-openobserve` -- acquire the values these keys point at.
- `diagnose-mechanism` -- consumes the joined picture.
