`CommandSegment` now records the control operator that ended it, as `terminator` (`;`, `&&`, `||`, `|`, `&`, ...; `None` for the last segment).

This lets a caller tell a harmless trailing `;` -- whose empty tail segment is not a second command -- from a trailing `&`, which backgrounds the command itself. The new latchkey permission-request guard uses it to allow `<request>;` while still blocking `<request> &`.
