`CommandSegment` now records the control operator that ended it, as `terminator` (`;`, `&&`, `||`, `|`, `&`, ...; `None` for the last segment).

This lets a caller tell a harmless trailing `;` -- whose empty tail segment is not a second command -- from a trailing `&`, which backgrounds the command itself. The new latchkey permission-request guard uses it to allow `<request>;` while still blocking `<request> &`.

New `command_basename(segment)` returns the command a segment invokes, with any path prefix stripped and any leading `VAR=value` env assignments skipped (`None` for an empty segment). It is how a caller tells an invocation from a mention of one: the lexer strips quotes, so a URL or command name inside another command's quoted argument otherwise looks identical to the real thing. The tk verb recognition already worked this way internally; this exposes the same rule.
