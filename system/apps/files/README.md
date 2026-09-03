# files

The workspace's file viewer: the app behind the rail's File Viewer row.

The server is [dufs](https://github.com/sigoden/dufs), a single static binary
installed at image build by `system/scripts/install_dufs.sh` (version and
per-arch sha256 pinned there). supervisord's `[program:files]` runs it over
`data/` -- the workspace's user-facing file tree -- bound to loopback with all
operations enabled (browse, preview, upload, rename, delete): the workspace
origin is what gates access, exactly as for every other registered app.

Beyond the manifest (`app.toml`, which `forward_port.py --manifest` registers
at service start together with the icon it names, `icon.svg`), this directory
holds `assets/`: a vendored
copy of dufs's own frontend (its `assets/` directory at the pinned release,
served via `--assets`), carrying two workspace patches -- a toolbox toggle
that hides "system files" (any path whose name, or any segment of a search
result's path, starts with `.`) by default, with the choice kept in the
browser's localStorage; and a location beacon that posts the path being
viewed one hop up (`window.parent.postMessage({type: "minds-location",
path})`) on each page load, so the workspace shell can reopen a file-viewer
instance at the folder it was looking at. Hiding is purely client-side: the
server lists everything, so flipping the toggle needs no reload and direct
navigation into dotted paths keeps working. The patched blocks are marked
with `minds patch` comments in `assets/index.js` / the
`.toggle-hidden-files` control in `assets/index.html`.

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
