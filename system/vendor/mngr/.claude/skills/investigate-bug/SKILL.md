---
name: investigate-bug
description: Investigate a user-facing minds bug end to end, locally and interactively, with full credentials -- classifying the failure, routing to the observability system that holds the evidence (hosted Sentry, Bugsink, OpenObserve), correlating across them, driving to a mechanism, and landing on either an actionable diagnosis or the logs that would settle it. Use whenever someone reports a minds desktop error (a screenshot, pasted error text, "user X hit Y") and you need to determine what happened and why.
---

# Investigating user-facing minds bugs

This is the **local composition**: a human is present, you hold real credentials, and you can query live.

The investigative method itself lives in four shared skills, and this skill is the order you run them in plus the parts that only make sense locally.
Invoke each shared step rather than working from memory -- they carry the tables, the taxonomy, and the discipline that this file used to hold inline, and they are kept current for both this consumer and the automated mngr-seer pipeline.

## The flow

```
  a report arrives
  (screenshot, pasted error, "user X hit Y")
        |
        v
  1. classify-failure ........ what kind of failure; which stores are expected-empty
        |
        v
  2. probe live server health .... local, no credentials needed
        |
        v
  3. acquire along the route ..... access-sentry / access-bugsink / access-openobserve
        |                          (+ the bug-report move, when the evidence is on the user's machine)
        v
  4. correlate-evidence ...... join on keys; establish what absences actually mean
        |
        v
  5. diagnose-mechanism ...... what state, input, or sequence makes this path fail
        |
        v
  6. calibrate-confidence .... actionable diagnosis, OR the logs that would settle it
        |
        v
  report to the human
```

Steps 1, 4, 5 and 6 are shared with the automated pipeline and contain no local assumptions.
Steps 2 and 3 are privileged and local, and are where this skill does its own work.

You will loop.
Step 4 routinely sends you back to step 3 for evidence you did not know you needed, and step 5 back to step 4.
That is the method working, not a failure of the order.

## Step 1 -- classify the failure

Invoke **`classify-failure`** with the exact error text, before opening any dashboard.

It returns the failure shape and, more importantly, which stores can and cannot hold this failure's evidence.
Carry the expected-empty list forward: it is what stops you reading server-side silence as a finding in step 4.

## Step 2 -- probe live server health

The connector's health endpoints are public and need no credentials.
With the tier's connector host from its `client.toml`:

```bash
dig +short <connector-host>
curl -s https://<connector-host>/health/liveness
curl -s https://<connector-host>/version
```

A healthy probe proves the server is up *now* and pins which deploy is live.
It says nothing about the incident window -- for that you need OpenObserve's access logs in step 3.

Skip this when step 1 classified the failure as one where no packet left the user's machine.
The server's health is not in question and probing it wastes a turn.

## Step 3 -- acquire evidence along the route

Route on what step 1 told you:

- **Client-side failure** -> `access-sentry`, plus the user's local logs.
  Search bug reports first -- they carry the user's email in the body -- then automatic events.
  The local log locations are defined in `apps/minds/imbue/minds/utils/sentry/core.py` (the desktop log inventory) and `libs/mngr/imbue/mngr/utils/logging.py` (the mngr CLI events log); read them at the release the user is running, since source history has the paths for older versions.
- **Server exception** -> `access-bugsink`.
- **Request timelines, "was the server serving?", a user's server-side activity** -> `access-openobserve`.
  The workhorse is the `http_request` access-log line.
- **Aggregate questions** ("how many users are affected over months") -> `access-analytics`, or a Sentry cohort search.

**The bug-report move.**
When the evidence is on the user's machine, the highest-yield step is asking the user to file an in-app bug report.
It packages their local logs and diagnostics, with the heavy parts landing in S3 and referenced from the Sentry event.
Manual reports are sent **even when the user has error reporting disabled**, which makes this the only route to a consenting user whose automatic events are gated off.

This move has no automated equivalent.
It is the clearest thing the local consumer can do that the pipeline cannot, and it is worth reaching for early rather than after the other routes are exhausted.

**Record what you searched and what came back empty**, including the filters.
Step 4 needs it, and reconstructing it later from memory is where investigations quietly go wrong.

## Step 4 -- correlate

Invoke **`correlate-evidence`**.

It owns the correlation keys, the client/server identity gap and the moves that get around it, and the rules for interpreting absence -- including the positive control, which you *can* run here and should.
Locally you have the standing that an air-gapped consumer lacks: if a zero is not yet trustworthy, go back to step 3 and make it trustworthy.

## Step 5 -- diagnose

Invoke **`diagnose-mechanism`** with the joined evidence and the source tree.

Note the release the error fired from; the skill uses the span between that release and current `main` as evidence in its own right, and will tell you if a fix has already landed.

## Step 6 -- calibrate, then report

Invoke **`calibrate-confidence`** before writing any conclusion.

It decides which of two artifacts you are entitled to produce: an actionable diagnosis with numbered fix options, or a logs proposal naming exactly what would settle the surviving hypotheses.
The second is a real outcome, not a failure.
It is what gives a stalled investigation somewhere to land, and the same artifact the automated pipeline produces, so it can be handled the same way.

Then report to the human in prose.
Lead with the artifact and the confidence.
Say plainly which absences your conclusion rests on and which of them you proved.

Because a human is present, you can stop and ask rather than guess.
Prefer that to a confident write-up whenever the next step turns on something they know and you do not -- which tier, which user, whether the behaviour is new.

## Access boundaries

Sentry is reachable through latchkey with no tunnel.
Bugsink and OpenObserve are self-hosted with loopback-only APIs: each session needs an SSH tunnel whose key and credentials come from the tier's Vault entries.
Vault grants are per-tier; the email-to-user-id lookup and the S3 bug-report attachments need their own grants.

## Living document

This suite is a living document.
When any of its information turns out to be stale -- a moved path, a changed API shape, a command that no longer works -- suggest to the user that you be permitted to file a Linear ticket capturing the correction, in the skills project at https://linear.app/imbue/project/946dd151-75bb-43fc-98a1-5544717be0f6/overview, and take it through the normal workflow states (e.g. In Progress while the skill is being fixed).
Ask before filing; do not silently work around rot.

A correction to one of the four shared steps lands for the automated pipeline too, which is the point of them being shared.
Say which skill the correction belongs to when you file.

## Related skills

The shared method, in order:

- `classify-failure` -- what kind of failure, and which stores can hold its evidence.
- `correlate-evidence` -- join across stores; decide what an absence means.
- `diagnose-mechanism` -- evidence plus source to a named failure mechanism.
- `calibrate-confidence` -- actionable diagnosis, or the logs that would settle it.

Acquisition, local and privileged:

- `access-sentry` -- hosted Sentry (desktop errors + bug reports).
- `access-bugsink` -- the per-tier Bugsink error tracker.
- `access-openobserve` -- the per-tier OpenObserve logs/metrics.
- `access-analytics` -- aggregate product metrics (not an incident tool).

Design: `specs/bug-investigation-pipeline/spec.md` explains why the method is split this way and how the mngr-seer pipeline composes the same four steps.
