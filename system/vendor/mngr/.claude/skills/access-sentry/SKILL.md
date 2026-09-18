---
name: access-sentry
description: Reach and search hosted Sentry.io for minds desktop-client errors and user-filed bug reports, through latchkey with no hand-managed tokens. Use when investigating a client-side minds failure, looking up a user's bug report, fetching a specific event id, or measuring how widespread a client-side error is.
---

# Accessing hosted Sentry (minds desktop client)

Hosted Sentry.io holds everything the minds desktop client reports: automatic errors plus user-filed bug reports.
It holds nothing server-side -- the Modal services report to Bugsink instead (`access-bugsink`).

## Scope and structure

There are two project families, split by platform, each with one project per environment (`production`, `staging`, `development`).
The Python family (slug pattern `minds-<env>`) receives the Python backend and the latchkey-forward daemon; the JavaScript family (slug pattern `minds-frontend-<env>`) receives the browser UI and the Electron shell.
Dev and CI installs report to the `development` projects.

## Access through latchkey

Sentry is a built-in latchkey service and needs no tunnel.
`latchkey services info sentry` should report `credentialStatus: valid`.
Discover the org slug and project ids at runtime instead of hardcoding them (the org slug is deliberately kept out of this repo):

```bash
latchkey curl -s 'https://sentry.io/api/0/organizations/'                       # -> the org slug
latchkey curl -s 'https://sentry.io/api/0/organizations/<org>/projects/?per_page=100'   # -> the minds-* project ids
```

On a 401/403 while the credential status is `valid`, suspect missing permissions, not an expired token; do not re-auth.

## Endpoints that matter

```bash
# Issue search (grouped by exception type + stack):
latchkey curl -s 'https://sentry.io/api/0/organizations/<org>/issues/?query=<q>&project=<id>&statsPeriod=90d'

# Discover: body-level full-text across events -- the workhorse:
latchkey curl -s 'https://sentry.io/api/0/organizations/<org>/events/?field=title&field=timestamp&field=message&field=user.id&query=<term>&project=<id>&statsPeriod=90d&per_page=100'

# Full latest event of an issue (exception values, tags, contexts, extras):
latchkey curl -s 'https://sentry.io/api/0/organizations/<org>/issues/<issue_id>/events/latest/'
```

Prefer the org-scoped endpoints with `project=<id>` filters.
`statsPeriod=90d` works there; the *project*-scoped issues endpoint rejects it.

## Search gotchas

1. Issues group by exception type and stack, so detail buried in the message never reaches the title.
   Errors that wrap a failed `mngr` subprocess carry the real error, and any email, in the stderr inside the event body.
   A title search for them returns zero even though the event exists; use Discover for anything body-level.
2. The per-issue events *list* endpoint truncates bodies, and its `query` param does not reliably search them.
   Do not trust a zero from it; fetch `events/latest/` or a specific event id instead.
3. Discover free-text matches inside exception values, but the `message` field it returns does not contain them.
   Grepping returned `message` fields client-side gives false zeros; fetch the full event to confirm content.
4. Before trusting any zero, run the positive-control check from `investigate-bug`.

## Bug reports (the user-submitted channel)

Find them with `query=manually_submitted%3Atrue`; titles start with `[bug report]`.
Every report is its own issue (random fingerprint), so search by the tag or by the event id the user was shown, never by grouping.
The user's email lives in the event body's extras at `app_diagnostics.signed_in_account_emails`; match it with Discover or a full-event fetch, never a tag query.
Manual reports are sent even when the user has disabled error reporting.
Heavy attachments are not stored in Sentry: they are S3 URIs in the event's `uploaded_files_*` extras, and reading them requires S3 access you must arrange yourself.
The inline description can be scrubbed to `[Filtered]` or truncated; the S3 copy is authoritative.

## Identity

Automatic events carry no email and no PII.
`user.id` is a random 32-hex install id shared by every surface of one install.
It has no mapping to the server-side SuperTokens user id.
Starting from an email, the only Sentry entry points are bug-report bodies and user-quoted event ids.

This skill is a living document; when information here is stale, follow the update protocol in `investigate-bug`.

## Related skills

- `investigate-bug` -- routing, correlation keys, and the rules for interpreting zeros.
- `access-bugsink` -- the server-side exception store.
- `access-openobserve` -- the server-side request logs.
