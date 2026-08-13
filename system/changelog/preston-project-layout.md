Update `system/scripts/layout.py`'s help for the workspace's views.

The workspace no longer shows a named layout. It shows one *view* at a time: either a project — a filter over the machine's chats, terminals, browsers, apps and pages, plus its own dockview arrangement — or `Everything`, the unfiltered home that every object appears in, including objects filed in no project at all. A connected browser client reports whichever view it has open, and that is the thing a layout op can target.

The `--layout` help and the module docstring now say so: mutating ops name the view a client is in (`context` reports it), `Everything` is a valid target even though it is not a project, and the older named layouts (`desktop` / `mobile`) still resolve for compatibility even though nothing keeps one active.

Nothing about the op set or the failure modes changed. A mutating op still fails with a clear error listing the connected clients when none of them has the named view open.

The `rename` op's help now says what rename actually does: it names the object machine-wide through the workspace's member-titles store, so the title shows in every view, not just one panel's tab.

Let an app register its own icon.

`system/scripts/forward_port.py --name <name> --url <url>` now accepts an optional `--icon` (the SVG markup itself) or `--icon-file` (a path read once, at registration time, whose *contents* are stored). The registry keeps the markup, not a path: a path would have to be readable, at render time, by every consumer of `apps.toml` — the system-interface server, the desktop client, anything reading it off a shared host — and those do not share a filesystem view with the service that registered.

Because the markup ends up inlined into the workspace's DOM, it is validated before it is stored: it must parse as exactly one well-formed `<svg>` element with nothing that executes (`<script>`, `on*` handlers, `javascript:` URLs), nothing that restyles the host document (`<style>`), nothing that embeds foreign content (`<foreignObject>`), and no reference to anything outside the icon itself. XML declarations, doctypes, comments and CDATA are refused outright rather than tolerated, and the markup is capped at 16384 characters so one app cannot bloat a registry every consumer re-reads on every change. A rejected icon fails the registration loudly instead of landing half-registered.

Registering without an icon flag leaves whatever icon the app already registered untouched, so a service that re-registers on every restart — the normal supervisord case — does not silently fall back to the generic glyph. Passing an icon again replaces it.
