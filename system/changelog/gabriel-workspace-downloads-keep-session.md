Downloads from the workspace's built-in pages work inside Imbue Studio. Before, downloading a folder or file in Files, an image from the chat lightbox, or the avatar chooser's original SVG offered to save `download.txt` (or the file's name with `.txt` in place of its extension), and the download failed.

- Imbue Studio frames the workspace from another site, so the workspace's session cookie is partitioned, and the browser sent a `download` link's request without it. The new shared `startDownload` helper in `workspace_ui` loads each download in a hidden frame instead, whose request carries the cookie.

- A download the server refuses now says why, in a toast or in the lightbox's title, instead of offering to save the refusal.
