# workspace-profile

Reads the workspace profile snapshot (`data/.state/profile/account.json`) that a client
outside the workspace -- the Imbue Studio desktop, or the hosted web chrome -- materializes
from the workspace's account, and resolves account-keyed settings for a request identity.

The snapshot is read-only inside the workspace: it names the workspace's account and owner
and carries the account-keyed settings the built-in apps consume (none yet; keybindings are
the first planned key). The workspace's own layer lives under `data/system/profile/`:
per-user override files (`overrides/users/<user_id>.json`, written by the workspace only)
and the shell's unsent account changes (`unsent.json`), which overlay the owner's snapshot
values until they are delivered to the account or discarded.

Resolution order for one setting, earliest wins: the requesting user's override, then (owner
only) the unsent changes, then (owner only) the snapshot's account value, then the default.
Owner detection follows the `X-Imbue-Identity` header exactly as the shell does: a request is
the owner's when the header says so, and the owner's user id is then the snapshot's owner id,
so an unshared workspace (whose header carries no user id) still keys the owner's overrides.

Every read is tolerant: an absent or unreadable snapshot (a workspace created by an older
client) resolves from overrides and defaults alone, and a snapshot a newer client wrote (a
higher `snapshot_format`, unknown keys) is still resolved from -- the workspace never writes
it anyway.

The contract with the desktop's writer is pinned cross-repo: the desktop publishes
`workspace_profile_contract.json`, `system/scripts/fetch_mngr_assets.sh` vendors it at the
pinned mngr commit, and `snapshot_contract_test.py` confronts this library's constants with
it.

```python
from pathlib import Path

from workspace_profile.identity import parse_identity_header
from workspace_profile.resolve import resolve_profile_setting
from workspace_profile.snapshot import load_workspace_profile_snapshot

root = Path("/home/user/workspace")
snapshot = load_workspace_profile_snapshot(root)
identity = parse_identity_header(request.headers.get("X-Imbue-Identity"))
value = resolve_profile_setting("some_setting", "default", snapshot=snapshot, identity=identity, workspace_root=root)
```
