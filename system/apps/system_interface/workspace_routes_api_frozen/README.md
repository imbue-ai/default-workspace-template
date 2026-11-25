# Frozen descriptions of the workspace routes

One copy of `../workspace_routes_api.json` per template release whose clients are still supported,
named for the release (`minds-v0.11.0.json`).

A release freezes the description by copying `../workspace_routes_api.json` here under its tag, in
the commit that is tagged: `just dwt-freeze-workspace-routes` in the mngr repository writes the copy
(its release procedure, `apps/minds/docs/deploy/ops/app-release.md`, step 6b), and
`.github/workflows/release-pin-gate.yml` fails a `minds-v*` tag that lacks it. A copy is deleted
when no supported client was written against that release any more.

`test_workspace_routes_description.py` holds the current description to every copy here: a route
keeps its path, its method and the name of its operation, no field of an answer is removed, and no
field of a request becomes required. A change that test refuses is not made to the route; it becomes a new route or a new
optional field instead.
