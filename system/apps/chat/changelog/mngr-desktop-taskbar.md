The chat's `new` launch path declares `text_param = "message"`, which makes it the launcher's primary free-text row: Enter in the desktop's field with no row matched starts a new chat whose first message is the typed text, in the chat's pinned window.

A new launch path, `send` ("Send to chat...") at `/send` with the same `message` text param, is the launcher's secondary row (Ctrl+Enter). The chat root handles `/send?message=<text>` on load and on `shell:navigate`: it opens a picker over the chats that are agents (a typeahead over their titles, one row highlighted, Enter or a click picks, Escape dismisses), sends the text to the chat picked through the ordinary send, selects that chat, and reports `/?chat=<id>` as its location so a reload sends nothing again; dismissing reports the selection alone, and a `send` with no text is a no-op that reports the selection.

The chat's e2e helper that started a chat from the launcher presses the menu's row instead of the retired tile.
