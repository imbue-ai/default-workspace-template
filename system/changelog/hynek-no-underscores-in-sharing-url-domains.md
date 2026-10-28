Newly minted service origin labels never contain an underscore: `forward_port.py` hyphenates the service name's underscores when it mints a label, so the workspace shell now registers as `system-interface-<rand>` instead of `system_interface-<rand>`, and a share link to it is `https://system-interface-<rand>.<workspace-domain>/`.

Browsers accept an underscore in a hostname, but `java.net.URI` (and so every JDK-based HTTP client and tool) refuses to parse such a host, and Python's `ssl` will not match a wildcard certificate against it, so share links to the shell were unreachable from a whole class of tools.

Labels that were minted before this change are left as they are: a label never changes once persisted, so existing share links, bookmarks and layouts keep working. Only a service registered for the first time (a fresh workspace, or a new app) gets a hyphenated label.
