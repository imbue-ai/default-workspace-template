Copy and paste works again over a shared link, and clicking into the Screen no longer pops a "Paste" button (Safari, Firefox) or a clipboard permission prompt (Chrome).

The viewer's calls to neko's routes (the clipboard text and image reads and the text write, the screencast fallback, the logout) carry the session token in the `token` query parameter, as its WebSocket already did, instead of an `Authorization: Bearer` header: the share gateway reads such a header as a visiting agent's admission token and refuses the request, and strips it from what it forwards, so over a share every paste failed with "The text could not be pasted on the Screen" and copying from the Screen failed silently.

The viewer no longer reads the local clipboard when its window gains focus; a paste (Ctrl+V, Cmd+V) is the one way text and images cross to the Screen, and its event carries the data with no permission at all.
