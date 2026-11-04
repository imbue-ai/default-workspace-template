Downloading a file or folder from Files works inside Imbue Studio. Before, it offered to save `download.txt` (or the file's name with `.txt` in place of its extension), and the download failed.

- Download loads the file through the shared `startDownload` helper rather than a `download` link, whose request went without the workspace's session.

- The server serves a file's `?download` as an attachment under its own name, alongside a folder's `?zip`. A file shared live from a computer gets the same treatment, but only when the computer actually sent the file.

- A download the server refuses shows a toast saying why, such as "Couldn't download notes.txt: There's nothing at /notes.txt." A missing path asked for as `?download` or `?zip` is answered in plain words, not with the not-found page.
