The File Viewer opens at the workspace folder (`/home/user/workspace/`) and can browse upward from there: dufs now serves the container's filesystem root instead of `data/`, so the breadcrumb reaches the workspace's own folders (`system/`, `skills/`, `docs/`) and anything above them that the workspace user can read. It stays bound to loopback with all operations enabled; the workspace origin is still what gates access.

- The manifest's `new` launch path is `/home/user/workspace/` (it was `/`), so a plain launch lands in the workspace folder.

- Folder paths given to the viewer are absolute now rather than paths under `data/`: `layout.py open files --path /home/user/workspace/data/notes/`, the `path` launch parameter (`--param path=/home/user/workspace/data/notes/`, a window at `/home/user/workspace/?path=...`), and the `open=files:` deep link.
