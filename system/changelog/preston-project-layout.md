Update `system/scripts/layout.py`'s help for the workspace's views.

The workspace no longer shows a named layout. It shows one *view* at a time: either a project — a filter over the machine's chats, terminals, browsers, apps and pages, plus its own dockview arrangement — or `Everything`, the unfiltered home that every object appears in, including objects filed in no project at all. A connected browser client reports whichever view it has open, and that is the thing a layout op can target.

The `--layout` help and the module docstring now say so: mutating ops name the view a client is in (`context` reports it), `Everything` is a valid target even though it is not a project, and the older named layouts (`desktop` / `mobile`) still resolve for compatibility even though nothing keeps one active.

Nothing about the op set or the failure modes changed. A mutating op still fails with a clear error listing the connected clients when none of them has the named view open.
