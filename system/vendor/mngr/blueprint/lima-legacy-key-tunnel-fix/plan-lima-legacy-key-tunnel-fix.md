# Plan: Fix the lima old-workspace tunnel failure (legacy shared key vs strict known_hosts)

> Fix the lima old-workspace tunnel failure (three parts): A) lima provider links the legacy shared root client key into the per-host keys dir when the fallback picks it, so the sibling known_hosts convention holds; B) pass known_hosts_path explicitly through host SSH info (host record/list JSON, HostSSHInfoEvent, RemoteSSHInfo) with sibling-derivation as fallback; E) make the desktop client's unattended recovery distinguish client-side transport failures from sick backends and back off instead of looping forever.
>
> * E is out of scope for this change: filed as mngr-internal#427 for the in-flight `recovery-backend-unreachable-inband` work.
> * A and B land together on `mngr/deploy-0-3-15`, riding (and extending the description of) PR #425.
> * A: the legacy keypair is symlinked (not copied) into the per-host keys dir, lazily inside `_root_ssh_keypair` whenever the fallback picks it, so every code path heals any old host on first touch.
> * B: an explicit optional `known_hosts_path` is threaded through host SSH info (list JSON `host.ssh` payload, `HostSSHInfoEvent`, forward's `RemoteSSHInfo`); forward prefers it, key-sibling derivation stays as the fallback.
> * B populates the field for every provider (not just lima), via the shared SSHInfo construction reading the existing `ssh_known_hosts_file` connector host data.
> * When the explicit path is present but missing on disk, forward falls back to the key-sibling and refuses only if that is missing too.
> * Lima populates the path for both root and non-root identities (it already passes the per-host known_hosts into `create_pyinfra_host` unconditionally).
> * Mixed-version events files are accepted: an old client hitting the new field warns and regenerates the events snapshot via the existing `DiscoverySchemaChangedError` path (events-format hardening is a separate branch).
> * A carries a `# CLEANUP:` marker (remove once lima hosts get per-host key rotation/adoption).
> * Forward's key-sibling fallback also carries a `# CLEANUP:` marker (remove once all supported producers emit the explicit path).
> * Forward's repeated identical tunnel-refusal warning is rate-limited, interval-based, per endpoint, at most once per 60 seconds.
> * The human-facing `SSHInfo.command` string gains `-o UserKnownHostsFile=... -o StrictHostKeyChecking=yes` when the path is known.
> * Verification: unit tests plus manual verification against the real old-lima-3 workspace from the new client (restart client, workspace connects, no recovery episode).
> * Plan lives in `specs/`-style committed form under `blueprint/lima-legacy-key-tunnel-fix/`.

## Overview

- The 0.3.15 client cannot tunnel into lima workspaces created before per-host client keys existed: `mngr forward` now hard-refuses to connect without a pinned host key, and it derives the known_hosts file as a sibling of the client key it resolved. For legacy lima hosts that key is the provider-wide `root_ssh_key`, which has no sibling known_hosts — lima deliberately renders pins per-host (localhost ports churn and get reused across VMs, so a shared pin file would go stale and collide).
- Fix A (targeted): make the sibling convention true for legacy lima hosts by symlinking the legacy shared keypair into the per-host keys dir the first time the fallback picks it. Per-host resolution then wins, and the key's sibling is the per-host pin file lima already maintains correctly.
- Fix B (class-level): stop making consumers guess by convention — carry the known_hosts path explicitly in host SSH info. Every provider already records it in connector host data (`create_pyinfra_host` → `ssh_known_hosts_file`); only the `SSHInfo` serialization drops it today. Forward prefers the explicit path, falling back to the sibling for old events.
- Part E (recovery misdiagnosing client-side failures as sick workspaces) is deliberately excluded: mngr-internal#427.
- Also: rate-limit forward's once-per-second identical refusal warning, and make the printed `ssh` command string verify pins.

## Expected behavior

- Opening a pre-0.3.15 lima workspace from the new client connects normally; no "Restarting" banner, no recovery episode, no warning flood.
- The heal is automatic and universal: any code path that resolves the legacy lima key (connect, discovery, forward) fixes the host's key layout on first touch; no operator action, no migration step.
- New lima workspaces are unaffected (their per-host key and pin file already sit together).
- Every provider's host SSH info (list JSON, discovery events) now names its known_hosts file explicitly; forward uses it directly, so future key-layout changes cannot silently break tunnel trust resolution again.
- Old clients reading a new-format events file log a schema-mismatch warning, regenerate the snapshot in their own schema, and continue (existing designed behavior; alternating old/new clients ping-pongs regeneration harmlessly).
- New clients reading old events (no explicit path) fall back to the key-sibling convention exactly as before.
- If the explicitly-named known_hosts file is missing on disk, forward falls back to the sibling; it refuses only when both are missing (still never trust-on-first-use).
- The `ssh` command string shown to users includes `-o UserKnownHostsFile=<path> -o StrictHostKeyChecking=yes` when the path is known, so a copy-pasted command verifies pins.
- Repeated identical tunnel-refusal warnings for the same endpoint are logged at most once per 60 seconds (with a suppressed-repeat count), instead of every second.

## Changes

- `libs/mngr_lima/imbue/mngr_lima/instance.py` (`_root_ssh_keypair`): when `resolve_keypair_with_fallback` returns the provider-wide legacy `root_ssh_key`, symlink the private and public key into `_host_keys_dir(host_id)` so per-host resolution wins from then on; `# CLEANUP:` remove once lima hosts get per-host key rotation/adoption.
- `libs/mngr/imbue/mngr/primitives.py` (`SSHInfo`): add optional `known_hosts_path: Path | None` (default `None`).
- The two `SSHInfo` construction sites — `libs/mngr/imbue/mngr/interfaces/provider_instance.py::_ssh_info_from_host` and `libs/mngr/imbue/mngr/api/discovery_events.py::_build_ssh_info_from_host` — populate it via the existing `get_ssh_known_hosts_file(host)` accessor, and extend the `command` string with the pin options when the path is known.
- `libs/mngr_forward/imbue/mngr_forward/ssh_tunnel.py` (`RemoteSSHInfo`, `_create_ssh_client`): add optional `known_hosts_path`; preference order explicit-if-exists → key-sibling → refuse; `# CLEANUP:` on the sibling fallback (remove once all supported producers emit the explicit path).
- `libs/mngr_forward/imbue/mngr_forward/snapshot.py` (`_parse_ssh_info`) and `stream_manager.py` (`_handle_host_ssh_info`): carry the optional field through from list JSON and `HostSSHInfoEvent`.
- `libs/mngr_forward/imbue/mngr_forward/server.py` (the tunnel-setup-failed warning): interval-based rate limit, per endpoint, at most one warning per 60 seconds, reporting the count of suppressed repeats.
- Unit tests: lima resolver heals the legacy layout (fallback → symlink → per-host wins, sibling known_hosts readable); forward preference order (explicit present / explicit-missing-falls-back / both-missing-refuses); snapshot/stream parsing with and without the field; command-string content; rate limiter (injected time source, no sleeps).
- Changelog entries for `libs/mngr`, `libs/mngr_forward`, `libs/mngr_lima`; extend PR #425's title/description.
- Manual verification: restart the new client, confirm old-lima-3 connects with no recovery episode.
