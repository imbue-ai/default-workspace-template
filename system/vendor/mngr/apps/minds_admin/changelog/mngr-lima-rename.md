Generic names for the slice identifiers that survive the gen-2 cutover (imbue-ai/mngr-internal#848).

- `server prep` / `setup` / `register` take `--slice-service-user` instead of `--lima-service-user`. It is optional and defaults per box generation: gen-2 boxes are pinned to `slicehost` (a differing override is refused, and a row still recording the pre-rename user is converged); gen-1 boxes default to the row's recorded user, else `limahost`.

- The gen-2 prep converges a box prepped before the rename: it hands everything `limahost` owned under `/srv/mngr-slices` to `slicehost` (owner, then group), removes the old user outright, and `prep` now stamps the row's service user (previously only `setup` did), so the connector's next box command SSHes as the new user.

- `bare_metal_servers.slice_service_user` and `pool_hosts.slice_instance_name` / `slice_disk_name` are the columns the admin tooling writes; the `lima_*` columns stay dual-written and every read falls back to them (`COALESCE`), all under `CLEANUP:` markers for once every tier's pool DB has migration 041. `pool list` reports `slice_instance_name` / `slice_disk_name`.

- The cutover state records (`CutoverWorkspaceState`, `CutoverPoolRow`) spell the names `slice_*`; state dirs written by pre-rename drills must be cleared (dev only).
