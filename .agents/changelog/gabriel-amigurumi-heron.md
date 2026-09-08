An `update-self` that advanced the vendored mngr left the workspace's `mngr` CLI
broken -- it exited 1 with `ModuleNotFoundError` -- which in turn broke agent
lifecycle detection and the reveal's own service restart. An editable install
pins the source path, not the dependency closure, so the moment a merge moved
`system/vendor/mngr` the CLI began running new code against the old resolution.
The `update-system-interface` reveal now refreshes the same three environments
`build_workspace.sh` builds -- the mngr tool, the backend tool, and the workspace
venv -- instead of only the backend tool. It keeps each tool's registered plugins
by reading them back from uv's own install receipt, so there is no second copy of
the plugin list to fall out of step, and it re-pins each tool to its in-tree
source so a drifted receipt cannot quietly swap the workspace's vendored code for
a published release.

Those installs now also target the installation that is actually on PATH. uv's
tool directory follows `$HOME`, and the workspace runs under a different `$HOME`
than the one the image was built with, so every previous refresh had been
rebuilding a second copy that nothing ever executed -- and reporting success.

Separately, a failed reveal now says *why*. The pre-flight boot's output was
going to `/dev/null`, so a rejected change reported only "merged backend failed
to boot in a pre-flight check", with the evidence already gone by the time anyone
read it. The boot's output now rides back on the failure and into the auto-revert
commit. A reveal that is going to fail also stops waiting as soon as the backend
has exited, rather than sitting out the full health deadline first.
