---
name: investigate-bug
description: Investigate a user-facing minds bug by routing to the observability system that actually holds the evidence -- hosted Sentry (desktop client errors + user bug reports), Bugsink (server-side exceptions), OpenObserve (server logs/metrics) -- correlating across them, and driving to root cause. Use whenever someone reports a minds desktop error (a screenshot, pasted error text, "user X hit Y") and you need to determine what happened and why.
---

# Investigating user-facing minds bugs

The observability systems split by where code runs, not by severity.
Route to the right store first, then dig.

## The evidence boundary

Everything hinges on one question: **did the failing operation's request ever leave the user's machine?**

| Evidence store | What it observes | What it can never hold |
|---|---|---|
| Hosted Sentry.io (`access-sentry`) | The desktop client -- automatic errors plus user-filed bug reports | Anything server-side; automatic events from installs with error reporting disabled |
| Bugsink (`access-bugsink`) | Unhandled exceptions and WARNING+ logs from the Modal services (connector, LiteLLM proxy) | Anything client-side; requests that never reached the server; raw request logs |
| OpenObserve (`access-openobserve`) | Per-request access logs and service logs/metrics from the server side | Anything client-side; requests that never reached the server; error grouping |
| The user's machine | The desktop client's and mngr's local logs | Nothing you can pull remotely -- the user must send it (see "the bug-report move" below) |
| Analytics (`access-analytics`) | Aggregate product metrics | Incident-level detail; it is not an investigation tool |

A failure that happens on the user's machine before any request is sent (DNS failure, offline, TLS, a crash in the client itself) leaves evidence **only** in Sentry and the user's local logs.
The server-side systems will be empty for that user at that moment.
That emptiness is *expected*, not exculpatory or incriminating on its own.

## Step 1 -- classify the failure from the error text

Read the exact error string before touching any dashboard.
Common shapes, most specific first:

- `[Errno 8] nodename nor servname provided, or not known` (macOS) or `[Errno -2] Name or service not known` (Linux): `getaddrinfo` failed -- the hostname never resolved, so **no packet left the machine**.
  Server-side stores are expected-empty.
  The cause is the user's network (VPN, captive portal, waking from sleep) or -- if many users hit it at once and the name fails to resolve from your machine too -- our DNS record.
- `connection refused` / `timed out`: the name resolved but the connect failed.
  Now the server side is genuinely in question.
- `could not reach the imbue_cloud connector at <url> after N attempt(s)`: the retrying client path -- transport failed repeatedly against a resolvable host.
- An `internal_error` response carrying an `event_id`: the connector's 500 handler embeds the Bugsink event id in the response body.
  Go straight to Bugsink with it.
- `MngrCommandError: mngr create failed (exit code N):` -- the desktop app wrapping a failed `mngr` subprocess.
  The real error is the embedded stderr, which lives in the Sentry event *body*, not the issue title.
- `Provider '<name>' is not available: <reason>` -- raised for imbue_cloud during provider discovery.
  The provider name embeds the user's slugified email (naming only); the hostname it tried to reach is the tier's `connector_url` from its `client.toml`.

## Step 2 -- probe live server health (no credentials needed)

The connector's health endpoints are public.
With the tier's connector host from `client.toml`:

```bash
dig +short <connector-host>
curl -s https://<connector-host>/health/liveness
curl -s https://<connector-host>/version
```

A healthy probe proves the server is up *now* and pins which deploy is live.
It says nothing about the incident window -- for that, query OpenObserve's access logs (below).

## Step 3 -- pull evidence along the route

- **Client-side failure** -> `access-sentry`, plus the user's local logs.
  Search bug reports first (they carry the user's email in the body), then automatic events.
  The local log locations are defined in `apps/minds/imbue/minds/utils/sentry/core.py` (the desktop log inventory) and `libs/mngr/imbue/mngr/utils/logging.py` (the mngr CLI events log); read them at the release the user is running -- source history has the paths for older versions.
- **Server exception** -> `access-bugsink`.
- **Request timelines, "was the server serving?", a user's server-side activity** -> `access-openobserve`.
  The workhorse is the `http_request` access-log line.
- **Aggregate questions** ("how many users are affected over months") -> `access-analytics`, or a Sentry cohort search.

**The bug-report move.**
When the evidence is on the user's machine, the highest-yield step is asking the user to file an in-app bug report.
It packages their local logs and diagnostics (heavy parts land in S3, referenced from the Sentry event).
Manual reports are sent **even when the user has error reporting disabled**.

## Correlation keys

| Key | Where it lives | Use |
|---|---|---|
| Bugsink `event_id` | `internal_error` response bodies | Direct jump from a client-visible 500 to the server exception |
| Deploy id | Sentry `release` tag, Bugsink `release`, `/version` | Pin which code was live |
| Environment | Sentry environment (`production`/`staging`/`development`); Bugsink environment (concrete env name); OpenObserve `minds_env` field | Keep tiers separate; dev/CI installs report to dev |
| SuperTokens user id (hyphenated UUID) | OpenObserve `http_request.user`, connector DB rows | The server-side identity key |
| `anonymous_user_id` (32-hex) | Sentry `user.id`; `~/.minds*/anonymous_user_id` on the user's machine | The client-side identity key; ties every desktop surface of one install together |
| Email | Manual bug-report bodies only; `uv run minds-admin account show <email>` returns the SuperTokens user id (operator-gated) | The starting point of most investigations |

The two identity spaces never meet server-side: the Sentry `anonymous_user_id` has no server-side mapping, and server-side ids never appear in automatic Sentry events.
You cannot get from an email to a user's automatic Sentry events, nor from a Sentry event to server logs, by identity alone.
The one manual bridge: a cooperating user can read `~/.minds*/anonymous_user_id` off their machine, which unlocks their automatic Sentry events.
Otherwise, bridge on time + deploy id + error shape instead.

## Interpreting absence -- before you trust any zero

1. **Run a positive control first.**
   Before concluding "user X / error Y is absent", run the same search for a token you *know* exists in the store (e.g. an error phrase you can see in another event's body).
   A zero is only meaningful after the search method has proven it reaches where the evidence would live.
2. **Consent gates automatic events only.**
   The `report_unexpected_errors` setting drops all automatic desktop events in-process (`_AutomaticReportingGate` in `libs/imbue_common/imbue/imbue_common/sentry/core.py`), but manual bug reports always send.
   So: no bug report means the user did not file one -- a real signal; no automatic events means consent-off *or* nothing happened -- ambiguous.
3. **Mind the retention windows.**
   Each store ages events out on its own schedule; Bugsink issue rows outlive their events.
4. **Mind the tier.**
   Dev and CI installs report to the `development` Sentry project.

## When the target user is invisible in a store

Two moves recover an investigation that identity cannot carry:

- Borrow a timestamp: find another instance of the same failure signature (a body-level Sentry search for the exact error phrase), then check server-side health at that instance's exact window -- this answers "was the server serving?" without ever locating the target user.
- Measure blast radius with the same phrase search, and read the distribution across flows as a signal: a failure that appears only in background retry loops points at transient client-side network state, not at the server.

## Access boundaries

Sentry is reachable through latchkey with no tunnel.
Bugsink and OpenObserve are self-hosted with loopback-only APIs: each session needs an SSH tunnel whose key and credentials come from the tier's Vault entries.
Vault grants are per-tier; the email-to-user-id lookup and the S3 bug-report attachments need their own grants.

## Living document

This suite is a living document.
When any of its information turns out to be stale -- a moved path, a changed API shape, a command that no longer works -- suggest to the user that you be permitted to file a Linear ticket capturing the correction, in the skills project at https://linear.app/imbue/project/946dd151-75bb-43fc-98a1-5544717be0f6/overview, and take it through the normal workflow states (e.g. In Progress while the skill is being fixed).
Ask before filing; do not silently work around rot.

## Related skills

- `access-sentry` -- reach and search hosted Sentry (desktop errors + bug reports).
- `access-bugsink` -- reach and query the per-tier Bugsink error tracker.
- `access-openobserve` -- reach and query the per-tier OpenObserve logs/metrics.
- `access-analytics` -- the aggregate product-metrics lakes (not an incident tool).
