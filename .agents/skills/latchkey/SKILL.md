---
name: latchkey
description: Use whenever you want to interact with third-party or self-hosted services (Slack, Google Workspace, Dropbox, GitHub, Linear, Coolify...) using their HTTP APIs on the user's behalf.
compatibility: Requires curl and the latchkey OpenHost app installed in this compute space.
---

# Latchkey

## Instructions

Latchkey is a credential-injection proxy: you send your HTTP request through it, and it adds the
user's stored credentials (OAuth tokens, API keys) before forwarding to the real service. You
never see the credentials. It runs as a separate OpenHost app in this compute space, reached
through the router's service interface.

Every call goes to `$LATCHKEY_GATEWAY` (already in your environment) and must carry the app
token header: `Authorization: Bearer $OPENHOST_APP_TOKEN`.

Use this skill when the user asks you to work on their behalf with services that have HTTP APIs,
like AWS, GitLab, Google Drive, Discord or others. Look for the newest documentation of the
desired public API online, and avoid bot-only endpoints.

## Examples

### Make an authenticated request

Prefix the real URL with `$LATCHKEY_GATEWAY/proxy/`:

```bash
curl -sS -H "Authorization: Bearer $OPENHOST_APP_TOKEN" \
  "$LATCHKEY_GATEWAY/proxy/https://slack.com/api/conversations.list"
```

Method, headers, query string, and body are forwarded; credentials are injected server-side.
(Do NOT add the service's own `Authorization` header — the gateway sets it.)

### Creating a Slack channel

```bash
curl -sS -X POST -H "Authorization: Bearer $OPENHOST_APP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-channel"}' \
  "$LATCHKEY_GATEWAY/proxy/https://slack.com/api/conversations.create"
```

### List services / check credentials

```bash
# All known services:
curl -sS -H "Authorization: Bearer $OPENHOST_APP_TOKEN" "$LATCHKEY_GATEWAY/services"

# One service's status (credentialStatus: missing / valid / invalid / unknown):
curl -sS -H "Authorization: Bearer $OPENHOST_APP_TOKEN" "$LATCHKEY_GATEWAY/services/slack"
```

If `credentialStatus` is not `valid`, the user has to connect the service first: send them to the
latchkey app's console at `https://latchkey.$OPENHOST_ZONE_DOMAIN/` and ask them to log in to the
service there, then retry.

### Ask for user permission

A `403` JSON response with `"error": "permission_required"` means this app has no grant covering
the request. The response includes a `grant_url`; requesting a specific grant yields one too:

```bash
curl -sS -X POST -H "Authorization: Bearer $OPENHOST_APP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"grant": {"scope": "discord-api", "permissions": ["discord-read-all"]},
       "return_to": "https://'"$OPENHOST_APP_NAME"'.'"$OPENHOST_ZONE_DOMAIN"'/"}' \
  "$LATCHKEY_GATEWAY/grants/request"
```

Then:

1. Post the returned `grant_url` to the user as a clickable link, with one short sentence saying
   what you want to access and why.
2. End your turn and wait. The user approves on the latchkey consent page and is redirected back.
3. When the user returns (or tells you to continue), retry the original request.

Available scope/permission names for a service come from `$LATCHKEY_GATEWAY/services/<name>`
(detent schema names, e.g. scope `slack-api`, permission `slack-read-all`). When not sure (and if
applicable), prefer the `*-read-all` permission variants as they are relatively safe and obvious.

### Git operations on GitHub (clone / fetch / push)

The gateway proxies GitHub's git smart-HTTP endpoints, so plain `git` works through it: point git
at the proxy URL and pass the app token header:

```bash
git -c "http.extraHeader=Authorization: Bearer $OPENHOST_APP_TOKEN" \
    push "$LATCHKEY_GATEWAY/proxy/https://github.com/<owner>/<repo>.git" <refspec>
```

(`clone`, `fetch`, and `ls-remote` take the same proxy URL and header.) The GitHub credential is
injected server-side — no token enters this container. This is gated by the `github-git` scope:
`github-git-read` covers clone and fetch, `github-git-write` covers push. Request them like any
other permission (see above). Only `https://github.com/<owner>/<repo>[.git]` URLs are supported;
prefer one-shot `-c` options over persisting the gateway URL or headers into git config.

## Notes

- A `400` from the gateway means the target URL doesn't belong to a known service or no
  credentials are stored — the message says which. Upstream errors (including upstream 403s) pass
  through with the upstream's own body.
- Unless the user explicitly asks about it, don't discuss Latchkey or the technical details (it's
  easy for the user to get confused).

## Currently supported services

Latchkey currently offers varying levels of support for the
following services: AWS, Calendly, Coolify, Discord, Dropbox, Figma, GitHub, GitLab,
Gmail, Google Analytics, Google Calendar, Google Docs, Google Drive, Google Sheets, Google Slides,
Linear, Mailchimp, Notion, Ramp, Sentry, Slack, Stripe, Telegram, Todoist, Umami, Yelp, Zoom, and more.

## Notion hack

Always use the `notion-mcp` latchkey service (via `$LATCHKEY_GATEWAY/services/notion-mcp`) rather
than the legacy plain `notion` one.
