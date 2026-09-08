# Per-machine latchkey credential stores

## What

Today minds keeps **one** latchkey credential store (`~/.minds/latchkey/credentials.json.enc`)
and pushes a permissions-filtered copy of it to every remote workspace's VPS.
That model has two defects:

* It breaks as soon as the user installs minds on a second computer -- the second
  install has a different store (and a different encryption key), so it cannot
  manage anything the first one created.
* A remote workspace cannot refresh its own OAuth tokens. The VPS gateway runs
  with `LATCHKEY_DISABLE_CREDENTIALS_REFRESH=1` precisely so it never races the
  desktop for a shared refresh token, which leaves the desktop as the only
  refresher -- and the desktop has to be online for a remote workspace's token to
  stay alive.

The target model: **every machine owns its own credential store.** The user's
computer owns one (shared by every local workspace, exactly as now); each VPS
owns one. A VPS refreshes its own tokens, offline from the desktop, because
nothing else holds the same refresh token.

Concretely:

1. A remote workspace's `mngr_latchkey/hosts/<host_id>/` directory becomes a
   usable `LATCHKEY_DIRECTORY` -- the *machine store*. It carries its own
   `credentials.json.enc`, and shares the desktop's `config.json`,
   `browser_state.json.enc` and encryption key by symlink.
2. The source of truth for a remote workspace's credentials **and** permissions
   is `~/.latchkey` on its own VPS. The machine store is a local mirror.
3. Managing a workspace's connectors or permissions is: pull the VPS state into
   the mirror, inspect/modify locally, apply the change on the VPS.
4. Local workspaces keep using the shared desktop store unchanged.
5. The app-level cross-workspace permission views go away; the per-machine
   Permissions tab is the only place grants are managed.

## Decisions taken

Recorded from the design discussion, in the discussion's own numbering where it
helps:

* **Per-workspace sign-in is accepted.** Connecting a service to a remote
  workspace means a browser sign-in for that workspace, producing an
  authorization distinct from every other machine's. That is what actually
  removes refresh-token contention; per-machine *files* alone would not.
  Splitting behaviour by credential kind (static credentials could still be
  copied freely) is a later optimization.
* **Mutations happen on the VPS.** Deletion is `latchkey auth clear` there;
  addition ships a single-service bundle and merges it into the VPS store. The
  desktop never uploads a whole store it downloaded earlier, so a token the VPS
  refreshed in the meantime cannot be reverted.
* **Each machine has its own encryption key, and transfers re-encrypt.** A
  mirror is always held under the *desktop's* key, so every machine store shares
  the desktop's browser session and the desktop can read all of them. Pulling
  re-encrypts the VPS store to the desktop key; pushing re-encrypts the addition
  to the VPS key, uploads it, and merges it there.
* **Directory layout stays where it is:** the machine store *is*
  `<latchkey_directory>/mngr_latchkey/hosts/<host_id>/`.
* **Version skew: higher version wins.** latchkey refuses a store stamped from
  the future, so an upload from a newer desktop to an older VPS fails loudly.
* **Account names need no reconciliation.** A rule's account reflects the user's
  intent, which does not change between computers.
* **Workspace backups do not include `~/.latchkey`.** Credentials are not backed
  up; a rebuilt VPS means reconnecting.
* **A workspace that is offline too long loses its tokens.** Same as a computer
  that is offline too long; acceptable.
* **Permissions move to the VPS too**, so a second computer can discover what the
  first one granted. Last write wins; the race is unlikely.
* **No aggregate cross-workspace views.** The app-level Settings pages that
  render them are removed.
* **Reads happen locally against the mirror**, not over the network.
* **The transition may invalidate some credentials.** Existing installs have the
  same refresh token on the desktop and on every VPS; once every VPS starts
  refreshing, rotating providers will invalidate the other copies. Users
  reconnect. No migration is written for this.

## Findings that constrain the design

These came out of reading upstream latchkey 3.8.0 (`dist/src/config.js`,
`encryptedStorage.js`, `playwrightUtils.js`, `cliCommands.js`) and the current
minds/mngr-latchkey code. They change what is buildable.

### F1. Browser state is encrypted with the directory's key

`browser_state.json.enc` is read and written through the same `EncryptedStorage`
-- hence the same encryption key -- as `credentials.json.enc`. A latchkey
directory can therefore share another one's browser state only if it shares its
encryption key, which rules out "each machine store holds a copy of its VPS's
store verbatim, under the VPS's own key".

**Resolution: a mirror is always re-encrypted to the desktop's key.** Each
machine keeps its own key (the VPS generates one at first provisioning and keeps
it; the desktop has the user's), and the key changes at the boundary:

* **Pull:** on the VPS, `latchkey auth re-encrypt <scratch>` runs with the VPS
  key in the environment and the *desktop* key on stdin; the result is
  downloaded and becomes the mirror. Every machine store is therefore readable
  by the desktop, and shares its browser session and encryption key by symlink.
* **Push:** the desktop re-encrypts the added service with the *VPS* key on
  stdin, uploads that bundle, and merges it into the VPS's own store there
  (F3), naming the services to take on the merge itself -- the scoping belongs
  where the write happens, so a bundle carrying more than was asked for cannot
  widen it.

The desktop consequently keeps a copy of each VPS's key (the VPS holds it only
in tmpfs, so a copy is needed to re-provision after a reboot anyway). It is a
per-host secret, kept beside the machine store rather than inside it, since the
machine store's own key is the desktop's.

A second computer needs no key from the first: it fetches the VPS's key from the
running VPS, then re-encrypts everything it pulls to its own local key. The one
unrecoverable combination is a VPS that rebooted (tmpfs wiped) while no computer
holds a copy of its key -- that workspace's credentials must be reconnected.

### F2. What the encryption key is load-bearing for, and what it is not

Two secrets ride next to the encryption key, and only one of them is derived
from it:

* The **gateway listen password** is an ordinary shared secret: the VPS gateway
  is *given* it (`LATCHKEY_GATEWAY_LISTEN_PASSWORD`, written to tmpfs by
  provisioning) rather than deriving it. It therefore stays the desktop-derived
  value on every machine even once their store keys differ, both gateways keep
  accepting the same value, and `desktop_gateway_proxy.mjs` can go on replaying
  the caller's password header when it forwards. No proxy change is needed.
* The **permissions-override JWT** signing key *is* derived from the gateway's
  encryption key (`derivePermissionsOverrideSigningKey`, an HMAC over it). A
  gateway running its own key therefore rejects a JWT the desktop signed.
  Post-rollout VPS agents send no override at all, so this only affects agents
  created before the one-gateway rollout, whose JWT was baked into their
  environment at creation and cannot be reissued. Those machines keep the
  desktop's key; every other machine gets its own. The rule retires with
  `_materialize_legacy_override_targets`.

### F3. `auth re-encrypt` refuses an existing destination

It errors with "Destination file already exists" rather than merging, so it
cannot add a service to a store that already has one. The "apply additions on the
VPS" decision depends on an upstream change here. That change should also carry
over per-service *preparations* (`savePreparation`), which the current command
copies alongside credentials.

### F4. `services info --offline` still reports `authOptions` and `setCredentialsExample`

Only the credential *validation* is skipped, so the connectors UI and the manual
credential form keep working off the mirror. The non-offline call is the one that
refreshes and rewrites the store -- it is how `_probe_expiring_credentials_for_remote_hosts`
renews tokens today -- so every read against a mirror must pass `--offline`.

### F5. Atomic writes clobber symlinks

Upstream writes both `browser_state.json.enc` and `credentials.json.enc` with
`writeFileAtomic` (write sibling, rename over). A rename replaces a symlink with
a regular file, silently forking the shared browser state on the first sign-in
run from a machine store. An upstream fix (resolve the link before renaming) is
planned; until it lands, machine-store sign-ins must not be relied on to update
the shared browser state. The same hazard applies to minds' own writes: nothing
may `atomic_write` over `hosts/<id>/config.json`.

### F6. Account selection travels with `latchkey curl`, not with the raw route

`--account` is a client-side option of the `latchkey` CLI, so a caller hitting
`/gateway/<url>` directly (the git smart-HTTP flow in the workspace skill) cannot
express an account and gets latchkey's default resolution. For `latchkey curl`
the account does reach the gateway, and the forwarding extension copies whatever
header carries it, so desktop egress selects the same account as a direct call --
today, because both stores are copies of each other. Per-machine stores are what
break that equivalence, not multiple accounts per se.

## Design

### Machine store layout

```
<latchkey_directory>/                     shared desktop store (unchanged)
  credentials.json.enc                    desktop credentials
  browser_state.json.enc                  shared browser session
  config.json                             hidden + custom service registrations
  encryption_key                          the user's key (K1)
  data-format-version
  mngr_latchkey/
    hosts/<host_id>/                      <- machine store: a LATCHKEY_DIRECTORY
      credentials.json.enc                mirror of the VPS store, under the desktop key
      data-format-version                 the mirror's own stamp   (owned)
      latchkey_permissions.json           canonical per-host policy (owned, pre-existing)
      vps_encryption_key                  the VPS's own key, used only at transfer time
      permissions.json          -> latchkey_permissions.json
      config.json               -> ../../../config.json
      browser_state.json.enc    -> ../../../browser_state.json.enc
      encryption_key            -> ../../../encryption_key
```

Symlinks are relative so the whole tree can be moved or copied. The mirror is
held under the desktop's key (F1), which is what lets the browser session and
the key itself be shared by link rather than copied.

`permissions.json` is linked to the file minds already maintains so that a bare
`latchkey` run against the machine store enforces the same policy the workspace
does.

A machine store is deliberately *not* a `Latchkey.initialize()` target: that
would run plugin migrations into a nested `mngr_latchkey/` and rewrite
`config.json` through the symlink (F5). Only the credential/service-introspection
subset of `Latchkey` may be used against one.

### Ownership and direction of travel

| State | Owner | Local copy | How it changes |
|---|---|---|---|
| Desktop credentials | desktop | n/a | browser sign-in / manual entry, desktop refresh |
| VPS credentials | VPS | machine store mirror (re-encrypted to the desktop key) | add: bundle re-encrypted to the VPS key, uploaded and merged there; remove: `auth clear` on the VPS; refresh: the VPS gateway itself |
| Per-host permissions | VPS | `latchkey_permissions.json` | edited locally, then uploaded; last write wins |
| Browser session | desktop | shared by symlink | any browser sign-in |

The desktop's push of credentials (`sync_credentials`) disappears. The desktop's
renewal loop (`_refresh_remote_credentials_until_shutdown` and friends) and the
VPS gateway's `LATCHKEY_DISABLE_CREDENTIALS_REFRESH=1` disappear with it.

### Flows

**Connect a service to a remote workspace.** From that workspace's Permissions
tab: browser sign-in (`POST .../permissions/connect-browser`) or the credential
form, both run on the desktop against the machine store -- shared browser state,
so usually one consent click -- producing credentials encrypted with the
desktop's key. That one service is
re-encrypted with the VPS's key, shipped, and merged into the VPS store (F3).
The mirror is then re-pulled so it reflects what the VPS actually holds.

**Disconnect.** `latchkey auth clear <service> --account <account>` runs on the
VPS. A workspace that is unreachable leaves the account connected there: the
request is retried, and the UI says so rather than claiming success.

**Grant / revoke a permission.** Unchanged locally (the gateway's `permissions`
extension edits `latchkey_permissions.json`), followed by an upload to
`~/.latchkey/permissions.json`. Uploads happen only when the content actually
changed, because baseline reconciliation and agent registration rewrite that file
on every discovery pass.

**Refresh.** Entirely the VPS gateway's business. The desktop learns about it the
next time it pulls the mirror.

**Read.** Everything the UI needs (`services info --offline`, `auth list
--offline`, the permissions file) is answered from the machine store, so opening
a workspace's connectors page costs no network round-trip.

### What the UI becomes

* The app-level Settings **Connectors** and cross-workspace **Permissions** views
  are removed, along with `permission_overview`'s cross-workspace machinery.
* The per-machine Permissions tab becomes the single surface, and reads the
  machine store rather than the desktop store for a remote workspace. Its
  "Sign out" verb becomes machine-scoped, since there is no longer a global
  credential to clear.
* The permission-approval dialog resolves credential status, accounts, and the
  manual-credential form from the asking workspace's machine store, and applies
  the resulting credentials to that workspace only.

### Desktop egress

`via-desktop` requests are served by the desktop gateway from the *desktop*
store, and checked against the desktop's copy of the host's permissions (F2, F6).
For now this rides on the assumption that a service used through desktop egress
is also connected on the desktop, with the same account. Two things must be true
for that to be acceptable: the failure must read as "also connect this on your
computer" rather than as a service outage, and the desktop's copy of the host
permissions must be refreshed often enough that a revocation made elsewhere is
not left open on the egress path (at minimum: on provisioning, on app start, and
after any local edit).

## Phases

Each phase is independently landable. Phases 3 and later are blocked on the
upstream latchkey work called out in F3/F5 and on the K1/K2 decision.

1. **Machine store layout** (done): materialize `hosts/<host_id>/` as a
   `LATCHKEY_DIRECTORY`, keep the credentials mirror populated from the existing
   push, and give callers a narrow way to talk to a machine store.
2. **Per-machine reads and writes** (done): the Permissions tab, its Add
   connection actions, and the approval dialog resolve a workspace's accounts,
   credential status and sign-ins against *that machine's* store
   (`machine_latchkey.py`), and the sync ships that store rather than a filtered
   copy of the desktop's. Sign out became machine-scoped with them.

   Reads could not move on their own, which the phase order originally assumed:
   with writes still landing in the desktop store, connecting a service for a
   remote workspace would have written somewhere the tab no longer looks and
   appeared to do nothing. Writes then force the push to ship the machine store,
   so all three moved together. A machine store that has never held anything is
   seeded once from the desktop's credentials, so existing remote hosts keep
   what they already had (`_seed_machine_store_from_desktop`, a marked
   one-release shim).
3. **Per-machine keys** (done): each machine keeps its own key, recorded once
   in its machine store (the machine's own copy is RAM-only, so the desktop's is
   the durable one) and handed to its gateway at provisioning; what is shipped
   to it is re-encrypted with that key while the mirror stays under the
   desktop's. Machines provisioned by an older build keep the desktop key (F2).
4. **Flip credential ownership** (done): the machine refreshes its own tokens
   (`LATCHKEY_DISABLE_CREDENTIALS_REFRESH` and the desktop renewal loop are
   gone), and the desktop reconciles rather than overwrites -- fetch what the
   machine holds, push the additions made here (a bundle merged there, F3),
   clear the removals made here and anything no longer granted, adopt the rest.

   Changes travel as *operations*, recorded in the machine's
   `credential_updates/` queue by whoever asks and applied in order by the
   supervisor that can reach it (`MachineCredentials.connect_service` /
   `.disconnect_account`). The UI waits for its request to clear before
   reporting the change, so what it says happened has happened; the request
   outlives the app, so a change asked for with the supervisor down is not
   lost. The periodic pass therefore has no
   intent to reconstruct: it adopts what the machine holds, and clears only what
   the machine's own permissions no longer grant.

   An earlier revision inferred the intent from a diff between the machine and
   this computer's cache, which needed a record of what the two last agreed on
   to tell "disconnected here" from "connected on another computer". Moving the
   mutations onto the source of truth removed the question.
5. **Flip permission ownership** (done): the machine's `permissions.json` is
   the policy it enforces, and each reconcile keeps whichever side was written
   last (modification times, since both sides run NTP and the losing case is
   the one last-write-wins already declines to adjudicate). A policy this build
   cannot parse is refused rather than adopted.
6. **Remove the cross-workspace UI** (done): the app-level Connectors, Local
   files and Machines sections and the routes behind them are gone, along with
   the overview builders that fed them. "Sign out" was rescoped to one machine
   in phase 2.

## Testing

* Unit: machine-store materialization (links point where they should, are
  relative, survive re-materialization, and the store's own files are never
  linked); mirror lifecycle across a permissions change that drops the last
  granted service.
* Unit: no Sentry/bug-report attachment glob matches anything inside a machine
  store. The plugin data dir is the attachment root and now contains credential
  stores two levels down; today's globs are non-recursive, and that must stay
  true deliberately rather than by luck.
* Integration: `sync_credentials` leaves the mirror byte-identical to what the
  VPS received.
* Later phases: a pull that observes a VPS-side refresh; an upload that is
  skipped because nothing changed; a disconnect against an unreachable VPS.

## Open questions

* Whether desktop egress keeps its "same connector on the desktop" assumption or
  gets a real answer (mirror-on-demand, or forwarding the credential).
* Whether a stale mirror is ever allowed to answer a *security* question (the
  desktop-side permission check on the egress path) or must be revalidated.
