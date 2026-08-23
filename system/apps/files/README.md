# files

The workspace's file viewer: the app behind the rail's File Viewer row.

There is no code here -- the server is [dufs](https://github.com/sigoden/dufs),
a single static binary installed at image build by
`system/scripts/install_dufs.sh` (version and per-arch sha256 pinned there).
supervisord's `[program:files]` runs it over `data/` -- the workspace's
user-facing file tree -- bound to loopback with all operations enabled
(browse, preview, upload, rename, delete): the workspace origin is what gates
access, exactly as for every other registered app.

This directory holds only the registered icon (`icon.svg`, passed to
`forward_port.py --icon-file` at service start) and this note.
