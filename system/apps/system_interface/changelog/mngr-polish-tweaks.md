The README's `layout.py open files --path` example now names a folder by its absolute path (`/home/user/workspace/data/notes/`): the file viewer serves the container's filesystem root and opens at the workspace folder, so a folder is no longer named relative to `data/`.

An app's page can now declare that it owns the close chord (Ctrl+W): the desktop then only tells the page, and leaves its window open. The browser uses this to close a tab rather than its window; every other app closes as before.
