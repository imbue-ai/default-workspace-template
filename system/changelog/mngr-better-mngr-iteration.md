The mngr pin in `pyproject.toml` may name a commit of the private mngr-internal repo on a branch iterating on a paired mngr change, not only the public mirror; `main` always pins the public mirror.

A build from a private pin reads a credential from `/run/secrets/mngr_internal_git_token` (a BuildKit secret on docker, an uploaded file on lima and modal) through the new `system/scripts/_mngr_git_auth.sh`, which scopes it to the commands that fetch mngr; the lima and modal templates delete the file right after the workspace build. A public pin builds exactly as before, with no BuildKit requirement: the `--secret` build arguments and Dockerfile `--mount=type=secret` lines exist only while the pin is private.

New `system/scripts/set_mngr_pin.py` moves the pin and those BuildKit lines together (`--public`/`--internal <commit>`) and checks that a tree agrees with itself (`--check`, `--require-public`). `list_mngr_plugins.py --kind` reports whether the pin is `public` or `internal`.

CI gains a `pin-is-public` job that refuses a private pin (meant to be a required check on `main`), the `test` job refuses one up front with the same script, and a new `release-pin-gate.yml` fails a `minds-v*` tag that carries one.
