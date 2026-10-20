A manifest's `[[message_handlers]]` now describe what a message means rather than who sends it, and can show a page instead of posting to a route.

- A message type is a lowercase prefix, `:`, and a kebab-case name (`minds:focus-chat`, `open:file`); `shell:` is refused, since it is the app contract's own prefix.

- A handler gives exactly one of `path` (a route of the app the shell posts the message to, as before) or `show` (a page template, such as `{path}?view`, that the shell fills from the message's fields and shows as a window of the app), with `showing` naming the app's other pages that already count as showing it.

- The reserved `handles` table is gone: a manifest that still carries it fails to load, like any unknown field.
