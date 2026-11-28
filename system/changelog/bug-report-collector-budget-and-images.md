The bug-report collector asks mngr only for the chats a report can keep: it ranks chats by when each one's transcript stream was last written and fetches those inside the recency window plus the newest up to the floor, where it used to fetch every agent's transcript and discard most of them. A chat whose stream it cannot locate is still fetched.

An image inlined into a chat (codex keeps the images its tools return as `data:` URLs) now rides as a short stub naming its type and size. The image bytes are no longer staged, scanned or shipped, so random runs inside them can no longer match a scanner rule and withhold the whole chat.

Given `--budget-seconds` (newer Imbue Studio passes it), the collector works to a deadline: each mngr call gets no more than the time left, gathering stops with a quarter of the budget kept for the secret scan, and the archive ships with whatever was gathered and a note naming what the time did not cover (chats not read, panes not captured), instead of the whole collection timing out with nothing sent. Chats are gathered before panes.

A chat the scan flags on specific lines now ships without just those records, each replaced by a `withheld` record and noted in `collection-notes.txt`, provided the rest of the chat passes a second scan; otherwise the chat is withheld whole as before. Logs are still withheld whole.
