# The `imbue_attribution` cookie contract

This document is the contract between the imbue.com marketing site and the
minds connector for marketing attribution. It covers everything the
marketing side needs to implement: how to set the cookie, and how to build
download and signup links. Share it with whoever owns imbue.com.

## How attribution works, in one paragraph

The marketing site sets a first-party cookie on `.imbue.com` recording how a
visitor arrived (campaign params, referrer). The minds signup surface
(`accounts.imbue.com`) and the download endpoint (`minds.imbue.com`) are
served under the same registrable domain, so the browser presents that
cookie when the visitor later creates an account -- including when they
first *download the desktop app* and sign up from inside it, since the app
opens the system browser (the same browser that visited imbue.com) to sign
in. The connector stamps the cookie's contents onto the new account, and the
`/download` endpoint records the same context per download, so the campaign
-> download -> signup funnel joins exactly on the cookie's visitor id.

The staging tier mirrors this layout on its own apex
(`accounts.imbue-staging.com` / `minds.imbue-staging.com` with a
`.imbue-staging.com` cookie), so the whole flow can be rehearsed end to end
against a staging deployment of the marketing site.

## Cookie requirements

- **Name**: `imbue_attribution`
- **Value**: percent-encoded JSON (i.e. `encodeURIComponent(JSON.stringify(payload))`).
- **Attributes**: `Domain=.imbue.com; Path=/; Secure; SameSite=Lax; Max-Age=7776000` (90 days).
- **Set server-side** (an HTTP `Set-Cookie` response header from the edge;
  imbue.com is deployed on Netlify, so this is a Netlify Edge Function) --
  NOT via client-side JavaScript. Safari's ITP caps script-written cookies
  at ~7 days, which would gut the attribution window on Safari.
- **Consent-gated**, matching the site's Google Consent Mode posture: an
  explicit banner choice is mirrored into a JS-written `imbue_consent`
  cookie (`granted`/`denied`) that the edge reads. With no explicit choice
  recorded, EEA/UK/CH visitors (geo-detected at the edge) get no cookie
  until they accept; visitors elsewhere are granted by default, same as the
  banner's defaults. An existing attribution cookie also counts as evidence
  of a prior grant (it was only ever set under one), so the edge keeps
  updating it even when the JS-written consent mirror has expired. An
  explicit `denied` expires any existing attribution cookie. Everything
  downstream degrades gracefully when the cookie is absent.
- **Keep it small**: the whole cookie must stay under 4 KB (browsers drop
  larger ones). The connector additionally ignores cookies over 8 KB and
  clamps individual fields (512 chars per field, 1024 for the raw query
  string, 64 for the visitor id).

## JSON payload schema

```json
{
  "v": 1,
  "id": "5f3a9c2e1b8d4e6f",
  "first": {
    "utm_source": "twitter",
    "utm_medium": "social",
    "utm_campaign": "launch-2026",
    "utm_term": "",
    "utm_content": "",
    "gclid": "",
    "fbclid": "",
    "ref": "https://t.co/abc",
    "path": "/minds",
    "q": "utm_source=twitter&utm_medium=social&utm_campaign=launch-2026",
    "at": "2026-08-13T17:04:05.123Z"
  },
  "last": { "...same shape as first...": "" }
}
```

Top-level fields:

- `v` (required, number): schema version. Currently `1`. The connector
  ignores cookies with any other version, so bump it only in coordination
  with a connector change.
- `id` (string): anonymous visitor id, minted randomly (e.g. 16+ hex chars)
  the first time the cookie is written and kept stable for the cookie's
  lifetime. It joins download events to signups; it must not encode any
  personal data.
- `first` (touch object): the visitor's first attributed touch. Written
  once, never overwritten.
- `last` (touch object): the most recent non-direct touch. Overwritten on
  each qualifying visit.

Touch object fields (all optional strings; omit empty ones to save space):

- `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content`:
  the standard campaign params, copied from the landing URL.
- `gclid`, `fbclid`: ad-click ids, copied from the landing URL.
- `src`: the marketing site's per-button spot tag (e.g.
  `src=modal-pr-review`), copied from the landing URL when present.
- `ref`: the `Referer` of the landing request.
- `path`: the landing page's path.
- `q`: the landing URL's full raw query string (future-proofing; the
  connector stores it verbatim, clamped to 1024 chars).
- `at`: ISO 8601 UTC timestamp of the touch, `Z`-suffixed (JavaScript's
  `toISOString()` output; connector-synthesized touches use the same
  format).

Fields the connector does not recognize -- and individual values that are
not strings -- are dropped at parse time; bad JSON, a non-object payload, or
a wrong version makes the connector treat the whole cookie as absent.
Either way, a malformed cookie never breaks a signup.

## Set/update rules for the edge function

On each request to imbue.com:

1. Classify the visit. A **non-direct touch** is a landing whose URL carries
   any of the campaign params (`utm_*`, `gclid`, `fbclid`), or whose
   `Referer` is an external site. A direct visit (no campaign params, no
   external referrer) is not a touch -- and neither is internal navigation
   carrying only a `src` spot tag (`src` rides along inside a qualifying
   touch but does not create one, or it would overwrite `last` on every
   internal click).
2. If consent allows (see "Consent-gated" above) and the visit is a
   non-direct touch:
   - No existing cookie: mint `id`, set `first` = `last` = this touch.
   - Existing cookie: keep `id` and `first`, overwrite `last` with this touch.
3. If consent allows and the visit is direct: re-emit the existing
   cookie unchanged (refreshing `Max-Age` keeps returning visitors inside
   the attribution window). Do not create a cookie for a direct first visit.
4. No consent: set nothing; on an explicit `denied`, expire any existing
   attribution cookie.

## Link formats

### Download buttons

Point download buttons at the connector's download endpoint so each
download is counted (and tagged, when the cookie is present):

```
https://minds.imbue.com/download?platform=mac
```

- `platform=mac` (or the precise `mac-arm64`): 302 to the macOS arm64
  `.dmg` the stable release channel serves, read from `stable-mac.yml`.
- `platform=source`: 302 to https://github.com/imbue-ai/mngr, the escape
  hatch for platforms without builds.
- Unknown or missing `platform`: 404. Coordinate new platform values with
  the connector before using them.

Campaign params -- and the site's per-button `src` spot tag -- may be
appended to the `/download` URL itself (`&utm_source=...&src=...`); they
tag the download event directly (overwriting the cookie's `last` touch, or
standing alone for a visitor with no cookie), so keep them on the link when
the button lives on a campaign landing page and keep the `src` tag for
per-spot funnel reporting.

### Signup links

Links that send a visitor straight to account creation should carry their
campaign params in the URL -- the signup page forwards them to the server,
which uses them even when the cookie is absent. The signup pages live on
the accounts host:

```
https://accounts.imbue.com/signup?utm_source=...&utm_campaign=...
```

## What the connector records (for reference)

- At account creation (and only then -- never on sign-in): one row with the
  visitor id, `first`, `last` (the signup page's own campaign params
  overwrite `last`), which surface the signup came from, and the signup
  method. See `migrations/026_account_attribution.sql`.
- At each `/download` hit: one event row with the visitor id, both touches,
  the platform, and the user agent. No IP addresses are stored.
