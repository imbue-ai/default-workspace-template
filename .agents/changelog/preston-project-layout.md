Document the app-icon flags in the build-app skill.

`forward_port.py` now takes an optional `--icon` (SVG markup) or `--icon-file` (a path whose contents are read at registration time), so an app can register the glyph the workspace draws for it. The CLI reference lists both, along with what the validation accepts — a single `<svg>` element, no script, style, event handler or external reference, at most 16384 characters — and the rule that omitting them leaves an already-registered icon alone.

The public-URL reference no longer claims `apps.toml` holds only `name` and `url`; it holds the service `label` and any registered `icon` markup too, and still never a public URL.

The layout-driving skills speak projects natively. `manage-layout`'s "Named layouts" primer is now a "Views" primer -- a view is a project or Everything, an op with no target goes to the view the connected client is looking at, `--view` addresses another one (`--layout` is the same flag under its old name), and the read ops take `--device desktop|mobile` for a view's per-device arrangement. The retired `for L in desktop mobile; do ... --layout "$L" ...; done` loops in build-app, caretaker, update-self, update-app, update-system-interface, and migrate-workspace collapse into single un-flagged calls that land in the user's current view.

`manage-layout` documents the new `layout.py views` subcommand for listing the machine's views (projects + Everything) with members and per-client presence.

`manage-layout` documents the corrected targeting rule for an op with no `--view`: it goes to your own view -- the one you were last messaged from, else your chat's project -- and only then to whatever the connected client is looking at, so a tab you open lands next to the chat that asked for it. It also explains the 412 that means the user has moved off your view. The `build-app`, `caretaker`, `update-self` and `update-system-interface` surfacing steps are reworded to match.
