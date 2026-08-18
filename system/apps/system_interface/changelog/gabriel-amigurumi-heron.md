Documentation fix: the README described the reveal step as refreshing only this
app's own uv tool (`uv tool install -e system/apps/system_interface --reinstall`).
It now refreshes every environment the served backend can start from -- the
vendored mngr tool the backend shells out to, this app's tool, and the workspace
venv -- because an editable install pins the source path but not the dependency
closure, so a merge that advances the vendored mngr would otherwise leave the
`mngr` CLI running new code against the old resolution.
