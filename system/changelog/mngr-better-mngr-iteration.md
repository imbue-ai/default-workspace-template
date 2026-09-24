The mngr pin in `pyproject.toml` may now name a commit of the private mngr-internal repo, not only the public mirror, so a template branch can build against an unmerged mngr change.

A build from a private pin reads a credential from `/run/secrets/mngr_internal_git_token` (a BuildKit secret on docker, an uploaded file on lima and modal) through the new `system/scripts/_mngr_git_auth.sh`; a public pin builds exactly as before.

This repo's CI refuses a private pin up front with a message saying why (it holds no credential by design), and a new `release-pin-gate.yml` fails a `minds-v*` tag that carries one.

`list_mngr_plugins.py --kind` reports whether the pin is `public` or `internal`.
