The op route's request models, the client, desktop, and window ids, the wallpaper reference, and the client-activity report now come from the shared `workspace_layout` library, so the shell and its callers read one definition; the route's behavior is unchanged. A client, desktop, or window id off its rule in a request is still answered with a 400.

- The op route's hints for an unknown or unsettled client name `workspace-layout context` instead of the removed `layout.py`.
