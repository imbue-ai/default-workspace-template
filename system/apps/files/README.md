# files

The workspace's file viewer: the `files` app of the desktop's launcher,
supervised as the `files` program declared in
`system/supervisord.conf.d/files.conf`. There is no Python package here: the
program line registers `app.toml` and the dufs port 8300 through
`system/scripts/forward_port.py`, then runs
`dufs --allow-all --bind 127.0.0.1 --port 8300 --assets system/apps/files/assets data`.

The server is [dufs](https://github.com/sigoden/dufs), a single static binary
installed at image build by `system/scripts/install_dufs.sh` (version and
per-arch sha256 pinned there). It serves `data/` -- the workspace's user-facing
file tree -- bound to loopback with all operations enabled (browse, preview,
upload, rename, delete): the workspace origin is what gates access, exactly as
for every other registered app.

## Opening a folder

The manifest's one launch path is `/` with an optional `path` param, so the shell
opens a file viewer window at `/?path=<folder>` (`layout.py open files --path
/notes/` is the same thing). dufs ignores the query; the vendored frontend
takes the frame to the folder itself (see below) and then reports that folder as
its location, so the window's stored path follows and a reload reopens the
folder.

## The vendored frontend

Beyond the manifest (`app.toml`) and its icon (`icon.svg`), this directory
holds `assets/`: a vendored copy of dufs's own frontend (its `assets/`
directory at the pinned release, served via `--assets`), carrying three
workspace patches -- a toolbox toggle that hides "system files" (any path
whose name, or any segment of a search result's path, starts with `.`) by
default, with the choice kept in the browser's localStorage; the `?path=`
redirect (a rooted path on this origin, honoured before anything renders); and a
location beacon that posts the path being viewed and the folder's name one hop up
(`window.parent.postMessage({type: "shell:location", path, title})`, the message
of desktop-interface contracts.md section 7; the title is the last segment of the
folder shown, or `Files` at the served root) on each page load, so the workspace
shell can reopen a file-viewer window at the folder it was looking at and title
it after the folder. Hiding is purely client-side: the server lists everything,
so flipping the toggle needs no reload and direct navigation into dotted paths
keeps working. The patched blocks are marked with `minds patch` comments in
`assets/index.js` / the `.toggle-hidden-files` control in `assets/index.html`.

When `install_dufs.sh` bumps the pinned dufs version, re-vendor `assets/` from
the new tag and re-apply the marked patches -- the vendored frontend and the
binary must come from the same release, since dufs's HTML placeholders and
listing JSON are version-coupled.

dufs serves the js/css/favicon with a year-long immutable cache under a path
that encodes only ITS version, so a change to the vendored assets is invisible
to any browser that has already loaded the viewer. `index.html` (which dufs
serves no-cache) therefore references the assets with a `?v=minds-N` query:
bump that revision in all three URLs whenever anything under `assets/`
changes, patch or re-vendor alike.

## Tests

`system/test_app_manifests.py` checks the manifest against the program line, and
`system/test_supervisord_layout.py` the program block.
