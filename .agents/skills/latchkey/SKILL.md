---
name: latchkey
description: Use whenever you need to call a third-party or self-hosted HTTP API on the user's behalf (Slack, Google Workspace / Gmail / Calendar / Drive / Docs / Sheets, Dropbox, GitHub, GitLab, Linear, Notion, Discord, Coolify, AWS, Stripe, Sentry, Zoom, ...). This is the default path for any auth-required external HTTP request -- prefer it over hand-rolling auth, manually setting bearer tokens, or asking the user for credentials. Also use this skill any time a `latchkey curl` request comes back with "Request not permitted by the user" -- you MUST send a permission request before retrying.
compatibility: Requires node.js, curl and latchkey (npm install -g latchkey). A desktop/GUI environment is required for the browser functionality.
---

# Latchkey

## Instructions

Latchkey is a CLI tool that automatically injects credentials into curl commands. Credentials (mostly API tokens) can be either manually managed or, for some services, Latchkey can open a browser login pop-up window and extract API credentials from the session.

**Use this skill any time you need to interact with a third-party HTTP API.** That includes the obvious cases (the user explicitly asks to do something on Slack / Gmail / GitHub / etc.) and the non-obvious ones (you need to fetch from a service to complete some other task). Reach for `latchkey curl` *before* trying raw `curl`, *before* trying to set `Authorization` headers yourself, and *before* asking the user for tokens. Latchkey is the canonical path; only fall back to other approaches if `latchkey services list` confirms the service is unsupported.

Usage:

1. **Use `latchkey curl`** instead of regular `curl` for supported services. Pass the **full URL** as a single argument (e.g. `latchkey curl 'https://gmail.googleapis.com/gmail/v1/users/me/profile'`). Do NOT use a `<service> <method> <path>` command-style form -- there is no such form, and it will produce a confusing "Could not extract URL from curl arguments" error.
2. **Pass through all regular curl arguments** - latchkey is a transparent wrapper.
3. **Check `latchkey services list`** to get a list of supported services. Use `--viable` to only show the currently configured ones.
4. **Use `latchkey services info <service_name>`** to get information about a specific service (auth options, credentials status, API docs links, special requirements, etc.).
5. **Permission requests are MANDATORY when a call is blocked.** Any time `latchkey curl` returns `{"error":"Error: Request not permitted by the user."}` (HTTP 403 from the gateway), you MUST send a permission request via `POST http://localhost:8000/api/permissions/request` and wait for the user's approval BEFORE retrying. This is also the right step when `latchkey services info <name>` shows `credentialStatus: missing` and you need access for an action. Do not silently fail, do not skip the call, do not assume it is a bug; the workspace is gating you on user consent. See the "Ask for user permission" example below.
6. **Look for the newest documentation of the desired public API online.** Avoid bot-only endpoints.


## Examples

### Make an authenticated curl request
```bash
latchkey curl [curl arguments]
```

### Creating a Slack channel
```bash
latchkey curl -X POST 'https://slack.com/api/conversations.create' \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-channel"}'
```

(Notice that `-H 'Authorization: Bearer` is not present in the invocation.)

### Getting Discord user info
```bash
latchkey curl 'https://discord.com/api/v10/users/@me'
```

### Ask for user permission (REQUIRED whenever a request is blocked)

If `latchkey curl` returns `{"error":"Error: Request not permitted by the user."}` (HTTP 403 from the gateway), or `latchkey services info <name>` shows `credentialStatus: missing` for a service you need to call, you MUST send a permission request first. Do not retry the curl without doing this -- the gateway will keep blocking until the user approves:

```bash
curl -XPOST http://localhost:8000/api/permissions/request \
  -H 'Content-Type: application/json' \
  -d '{"request_type": "LATCHKEY_PERMISSION", "service_name": "discord", "rationale": "I would like to access your Discord account to read server and channel information so I can help you summarize conversations."}'
```

A successful submission returns `{"ok":true,"event_id":"evt-..."}`. After that, surface a brief "permission request sent -- please approve in the desktop client" message to the user and wait for the system message confirming approval before retrying the original `latchkey curl` call.

The `service_name` value should be the latchkey service name (`google-gmail`, `slack`, `github`, `discord`, etc. -- match the names in `latchkey services list`). The `rationale` is a one-paragraph human-readable explanation of why you need access; write it for the user.

### Detect expired credentials and force a new login to Discord
```bash
latchkey services info discord  # Check the "credentialStatus" field - shows "invalid"
latchkey auth browser discord
latchkey curl 'https://discord.com/api/v10/users/@me'
```

Only do this when you notice that your previous call ended up not being authenticated (HTTP 401 or 403).

### List usable services

```bash
latchkey services list --viable
```

Lists services that either have stored credentials or can be authenticated via a browser.

### Get service-specific info
```bash
latchkey services info slack
```

Returns auth options, credentials status, and developer notes
about the service. If `browser` is not present in the
`authOptions` field, the service requires the user to directly
set API credentials via `latchkey auth set` or `latchkey auth
set-nocurl` before making requests.


## Storing credentials

Aside from the `latchkey auth browser` case, it is the user's responsibility to supply credentials.
The user would typically do something like this:

```bash
latchkey auth set my-gitlab-instance -H "PRIVATE-TOKEN: <token>"
```

When credentials cannot be expressed as static curl arguments, the user would use the `set-nocurl` subcommand. For example:

```bash
latchkey auth set-nocurl aws <access-key-id> <secret-access-key>
```

If a service doesn't appear with the `--viable` flag, it may
still be supported; the user just hasn't provided the
credentials yet. `latchkey service info <service_name>` can be
used to see how to provide credentials for a specific service.


## Notes

- All curl arguments are passed through unchanged
- Return code, stdout and stderr are passed back from curl
- Credentials are always stored encrypted and are never transmitted anywhere beyond the endpoints specified by the actual curl calls.

## Currently supported services

Latchkey currently offers varying levels of support for the
following services: AWS, Calendly, Coolify, Discord, Dropbox, Figma, GitHub, GitLab,
Gmail, Google Analytics, Google Calendar, Google Docs, Google Drive, Google Sheets,
Linear, Mailchimp, Notion, Sentry, Slack, Stripe, Telegram, Umami, Yelp, Zoom, and more.

### User-registered services

Note for humans: users can also add limited support for new services
at runtime using the `latchkey services register` command.
