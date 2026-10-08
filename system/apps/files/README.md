# files

The workspace's file viewer: the `files` app of the desktop's launcher,
supervised as the `files` program declared in
`system/supervisord.conf.d/files.conf`. There is no Python package here: the
program line registers `app.toml` and the dufs port 8300 through
`system/scripts/forward_port.py`, then runs
`dufs --allow-all --bind 127.0.0.1 --port 8300 --assets system/apps/files/assets /`.

The server is [dufs](https://github.com/sigoden/dufs), a single static binary
installed at image build by `system/scripts/install_dufs.sh` (version and
per-arch sha256 pinned there). It serves the container's filesystem root, so a
viewer opened at the workspace can walk up to anything the workspace user can
read; it is bound to loopback with all operations enabled (browse, preview,
upload, rename, delete): the workspace origin is what gates access, exactly as
for every other registered app.

## Opening a folder

The manifest's one launch path is `/home/user/workspace/` with an optional `path`
param (an absolute path, since dufs serves the filesystem root), so a plain launch
opens the workspace folder and the shell opens a file viewer window at
`/home/user/workspace/?path=<folder>` when asked for one (`workspace-layout open files
--path /home/user/workspace/data/notes/` is the same thing). dufs ignores the query; the vendored frontend
takes the frame to the folder itself (see below) and then reports that folder as
its location, so the window's stored path follows and a reload reopens the
folder.

## Opening a linked file or folder

The manifest also registers the `open:file` message (`[[message_handlers]]`,
desktop-interface contracts.md section 5.6), which the shell sends when a link to
a file or folder is clicked in any app (a `file:` URL the app contract hands it,
such as a chat's link to an absolute path). The shell shows the path's
`?view` page in a file viewer window: dufs shows a file there (text as
read-only source, PDFs and media embedded, anything else offered as a
download) and ignores `?view` on a folder, which lists as usual. A window
already on that file or folder, as `/a/b`, `/a/b/`, or `/a/b/?view` (where a
click through the listing leaves it), is raised instead of opening another.

## The vendored frontend

Beyond the manifest (`app.toml`) and its icon (`icon.svg`), this directory
holds `assets/`: a vendored copy of dufs's own frontend (its `assets/`
directory at the pinned release, served via `--assets`), carrying seven
workspace patches -- the app contract (below); a toolbox toggle that hides "system files" (any path
whose name, or any segment of a search result's path, starts with `.`) by
default, with the choice kept in the browser's localStorage; the `?path=`
redirect (a rooted path on this origin, honoured before anything renders); a
location beacon that reports the path being viewed and the name of the folder or
file shown (the contract's `location`, desktop-interface contracts.md section 7;
the title is the last segment of the
path shown, or `Files` at the served root) on each page load, so the workspace
shell can reopen a file-viewer window where it was looking and title it after
what it shows; file links that open in workspace windows; an Edit button on a
file's view page; and a phone layout (below). Hiding is purely client-side: the
server lists everything, so flipping the toggle needs no reload and direct
navigation into dotted paths keeps working. The patched blocks are marked with
`minds patch` comments in `assets/index.js` / the `.toggle-hidden-files` and
`.edit-file` controls and the `shell.js` script in `assets/index.html`.

The viewer speaks the app contract every app speaks (desktop-interface
contracts.md section 7), so its links follow the contract's link rule too:
`assets/shell.js`, ours rather than dufs's, imports the contract module the
shell builds and connects, and the patched scripts act through that connection
(`window.mindsShell`). Every other app serves the module at
`/_static/app_contract.js`; the viewer cannot add a route to dufs, but dufs
serves the filesystem root, so the script imports the module from its path in
the workspace, which dufs revalidates on every load like any file. The "not
found" page below loads it the same way, for its beacon.

The phone layout (`docs/system/blueprint/desktop-interface/plan-phone-interface.md`,
"The file viewer") takes over under 700px of the frame's own width, live across a
resize: the table and toolbox give way to a header (up, the folder's name, search,
and a kebab holding the toolbox verbs), a scrollable breadcrumb strip that dufs's
search bar replaces while searching, sort keys, and one row per entry (octicon,
name, and "<mtime> · <size>" beneath) whose kebab opens a sheet of the table's
actions. A tapped file opens in place, on its edit page (its view page where the
table offers no Edit), rather than in a window of its own as a click in the table
does (below); the editor page gets a Save button and the file's kebab, and a
view page the kebab with its Edit control. It is all in
its own `assets/phone.js` and `assets/phone.css`, included by the two
`minds patch` lines in `assets/index.html`. It drives dufs's own state and
functions (`DATA`, `PARAMS`, `movePath`, `deletePath`, `saveChange`, the toolbox
controls' handlers, the breadcrumb and search bar dufs built) and wraps `ready()`
rather than editing it, so re-applying it after a dufs bump is those two lines;
its test then checks that the names it leans on still behave.

A folder in a listing opens in place, as dufs ships it. A file does not: framed by
the shell, a click on a file's name in the table opens the file's `?view` page (dufs's
read-only view: text as source, PDFs, images, audio and video embedded, anything
else offered as a download) in a file-viewer window of its own, and the Edit
button opens its `?edit` page, each through the app contract's `shell:open`
with `ifPresent: "focus"` (contracts.md section 7), so a window already on that
page is raised instead of a second one opening. A modified or middle click does
the same. dufs renders these anchors with `target="_blank"`, which inside the
desktop would open a bare browser window; the patch cancels the click and asks
the shell instead, and leaves the anchors as they are, so opened on its own in a
browser tab the viewer behaves as dufs ships it. A file's view page offers Edit
wherever the listing would (uploads and deletes allowed), and it takes that same
page, and so its window, to the edit page.

`assets/404.html` is the File Viewer's own "not found" page, not part of dufs's
frontend, so re-vendoring leaves it alone: dufs serves a `404.html` found in its
`--assets` directory, with status 404, for every path that does not exist (it
answers a client it takes for a script, such as curl, with a bare "Not Found"
instead). dufs serves it as it is, with no placeholders filled in, so the page
reads the path asked for from its own address, names it, links the nearest
folder above it that exists (asking each folder's `?json` listing, whose
`dir_exists` says whether it is there), and posts the location beacon, titled
after the missing name.

When `install_dufs.sh` bumps the pinned dufs version, re-vendor `assets/` from
the new tag and re-apply the marked patches -- the vendored frontend and the
binary must come from the same release, since dufs's HTML placeholders and
listing JSON are version-coupled.

dufs serves the js/css/favicon with a year-long immutable cache under a path
that encodes only ITS version, so a change to the vendored assets is invisible
to any browser that has already loaded the viewer. `index.html` (which dufs
serves no-cache) therefore references the assets with a `?v=minds-N` query:
bump that revision in every asset URL (the favicon, `index.css`, `index.js`,
`phone.css` and `phone.js`) whenever anything under `assets/` changes, patch or
re-vendor alike.

## Tests

`system/test_app_manifests.py` checks the manifest against the program line, and
`system/test_supervisord_layout.py` the program block. `test_phone_layout.py`
(marked `browser`) serves `assets/` from a stand-in for dufs (`conftest.py`) and
drives the phone layout in Chromium: rows, the sheets' actions reaching dufs's
handlers (the page's `fetch` is stubbed and the requests dufs makes are
asserted), sort, search, and the table at a desktop width.
