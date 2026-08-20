# oauth_redirector

A fixed, per-tier OAuth callback redirector deployed as a tiny Modal Function.

## Why it exists

Google Web-application OAuth clients require every redirect URI to be
registered exactly (no wildcards). Production and staging register their
stable accounts domain directly, but dev and CI connectors live at per-env
`*.modal.run` hostnames -- dev envs would need a dashboard edit per env, and
CI envs are created dynamically, so per-env registration is impossible there.

The redirector closes that gap: each dev/CI tier registers exactly ONE
redirect URI -- this app's URL -- and the connector (see
`accounts_web.py`'s `OAUTH_REDIRECTOR_URL` handling) hands that URL to Google
as the `redirect_uri`. When the provider calls back, the redirector forwards
the entire callback query string to the per-env connector callback URL
carried in the OAuth `state` JWT's `cb` claim.

## Security model

The redirector holds **no credentials** (the only baked values are the
allowlist regex and an optional error-reporting DSN, below) and does not
verify the state JWT (each
env has its own signing key; the connector verifies signature + nonce-cookie
binding at the callback). Its one job is to not be an open redirector: the
`cb` claim is read unverified and then checked against a strict allowlist
baked in at deploy time --

- the URL must be `https`,
- its host must match `OAUTH_REDIRECTOR_ALLOWED_HOST_REGEX` (the tier's
  connector hostname pattern, e.g. `^minds-dev(-[a-z0-9-]+)?--rsc-dev-api\.modal\.run$`),
- its path must be exactly the connector's registered callback path.

Even a forwarded-to-the-wrong-env code is useless without that env's client
secret and the victim's nonce cookie, but the allowlist keeps the redirector
from bouncing codes anywhere outside the tier at all.

## Deploying (once per tier)

Not part of `minds-admin env deploy` -- the redirector is tier-level, not
env-level, and changes rarely:

```bash
just deploy-oauth-redirector dev   # or ci
```

The recipe bakes the tier's allowed-host regex at deploy time and prints the
deployed URL. It also best-effort reads the tier's Bugsink DSN
(`OAUTH_REDIRECTOR_SENTRY_DSN` from `secrets/minds/<tier>/sentry`) and bakes
it in for error reporting; when Vault is unavailable or the entry is
unpopulated the DSN is empty, which simply disables reporting -- the app's
zero-Vault deployment story is preserved. The function keeps one always-warm container
(``min_containers=1``): the redirector sits between Google's consent screen
and the connector callback in every Google sign-in, and a cold boot there
(4-34s measured) reads as "Google is slow" to the user. Register that URL as the sole redirect URI on the tier's shared
Google client, and set it as `OAUTH_REDIRECTOR_URL` in the tier's `sharing`
Vault entry.
