The create-theme skill describes theme contract 2's colors (keys, ramps, roles, computed hover) and the `migrate` command for themes made before it. build-app's font defaults are the brand's faces.

`update-self` carries the System Monitor frontend's bundle like the Style Guide's: a change under `system/apps/system_monitor/frontend/` rebuilds, snapshots and verifies it, and a worker hands it in with `--worker-bundle system_monitor=<path>`.
