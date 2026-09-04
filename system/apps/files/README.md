# files

The workspace's file viewer: the app behind the rail's File Viewer row,
supervised as the `files` program in `system/supervisord.conf`, which runs the
`files-app` entry point of this package (installed as its own uv tool by
`system/scripts/build_workspace.sh`, like every Python app with a manifest).

The server is [dufs](https://github.com/sigoden/dufs), a single static binary
installed at image build by `system/scripts/install_dufs.sh` (version and
per-arch sha256 pinned there). `files-app` (`src/files_app/main.py`) is the
`app_instances` sidecar launcher around it: it serves the instances API on
`127.0.0.1:8301` (the manifest's `instances_url`), registers `app.toml` and the
dufs port 8300 through `system/scripts/forward_port.py`, and runs
`dufs --allow-all --bind 127.0.0.1 --port 8300 --assets system/apps/files/assets data`
as its child, forwarding `SIGTERM` and `SIGINT` to it and exiting with its
status. dufs serves `data/` -- the workspace's user-facing file tree -- bound
to loopback with all operations enabled (browse, preview, upload, rename,
delete): the workspace origin is what gates access, exactly as for every other
registered app.

## Instances

The files row of `docs/system/blueprint/workspace-app-model/contracts.md`
section 4.3, served by the library's `JsonStoreInstanceSource` over
`data/.apps/files/instances.json` (`files-app --store` overrides it): an
instance is a `files-<N>` key and the path its page was last at, titled
`File Viewer <N>`, `idle`, `referenced` (the shell deletes it once no project
and no client layout references it, from phase 7 of the model on), never
renamed.

- `new` (optional `path`) allocates the lowest free number and stores the path
  (default `/`) as the instance URL under the dufs origin.
- Delete drops the record; the number is reused.
- A location report replaces the URL, which is what makes a file browser reopen
  at the folder it was showing.

## The vendored frontend

Beyond the manifest (`app.toml`) and its icon (`icon.svg`), this directory
holds `assets/`: a vendored copy of dufs's own frontend (its `assets/`
directory at the pinned release, served via `--assets`), carrying two
workspace patches -- a toolbox toggle that hides "system files" (any path
whose name, or any segment of a search result's path, starts with `.`) by
default, with the choice kept in the browser's localStorage; and a location
beacon that posts the path being viewed one hop up
(`window.parent.postMessage({type: "shell:location", path})`, the message of
contracts section 10) on each page load, so the workspace shell can reopen a
file-viewer instance at the folder it was looking at. Hiding is purely
client-side: the server lists everything, so flipping the toggle needs no
reload and direct navigation into dotted paths keeps working. The patched
blocks are marked with `minds patch` comments in `assets/index.js` / the
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

## Tests

`uv run pytest system/apps/files` from the repo root. `main_test.py` pins the
wiring; `test_files_app.py` runs `files-app` as a process around a fake `dufs`
(`testing.py`) and drives the instances routes.
