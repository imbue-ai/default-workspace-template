# files

Files: the workspace's file browser, the `files` app of the desktop's launcher,
supervised as the `files` program declared in
`system/supervisord.conf.d/files.conf`, which runs the `files-app` entry point of
this package (`src/files_app`). It starts dufs on loopback port 8301
(`dufs --allow-all --bind 127.0.0.1 --port 8301 --assets system/apps/files/static /`),
serves the Files server in front of it on the app's port 8300, and registers
`app.toml`.

The server is [dufs](https://github.com/sigoden/dufs), a single static binary
installed at image build by `system/scripts/install_dufs.sh` (version and
per-arch sha256 pinned there). It serves the container's filesystem root, so a
window opened at the workspace can walk up to anything the workspace user can
read; it is bound to loopback with all operations enabled (browse, preview,
upload, rename, delete): the workspace origin is what gates access, exactly as
for every other registered app.

## The Files server

`server.py` relays every request to dufs and answers four things dufs cannot:

- `/__files/api/connections`: GET lists what the user has shared with the
  workspace from their computers, POST asks for more. Both go through the
  latchkey gateway (`gateway.py`), whose address and credentials every
  supervised program inherits. A share is a file-sharing permission the
  workspace holds (`minds-file-server-read-<path>`, and `-write-` beside it when
  changes are allowed); `connections.py` reads them per device, notes whether
  Imbue Studio keeps a synced copy at `~/synced_folders/<device id>/<path>`
  (two-way with write access, one-way with read), and, since the workspace
  learns nothing from the sync itself, compares the copy's names with the
  computer's to say when a copy has fallen behind. A request is a
  `file-sharing` permission request from the workspace's main agent
  (`MNGR_AGENT_ID`); Imbue Studio shows its sharing dialog on the computer.
- `/__files/api/schema`: the OpenAPI document of the app's routes, built from its
  url map (`system/libs/app_schema`); the manifest's `api_schema_path`, which the
  visiting-agent primer links.
- `/__remote/<device id>/<path>`: a share with no copy here, read live from the
  computer's file server over WebDAV and answered in dufs's own shape (a page,
  `?json`, the bytes, PUT, DELETE, MKCOL), so the frontend browses it like any
  folder. Renaming is not offered there (the computer's file server has no
  MOVE). The file server's paths carry no device id (the permissions match
  `/minds-api-proxy/api/v1/files/<path>`); the device goes in the
  `X-Latchkey-Device` header.
- Every answer's `Content-Disposition` is made ASCII-safe. dufs names a file in
  it as it is, and a name with a character outside Latin-1 (the narrow space
  macOS puts in screenshot names) makes the forwarder between the workspace and
  the browser answer with an error in place of the file.

## The frontend

`frontend/` is a Mithril app on the shared `system/libs/workspace_ui` library,
one package of the npm workspace at `system/package.json`. Its vite build writes
`static/` (gitignored), the directory dufs is handed as `--assets`, and which the
Files server fills for shared items' pages. dufs serves
that one `index.html` for every folder and every file page, filling in the
page's data (base64 JSON in `#index-data`: a folder's listing, or the facts of
the file a `?view` / `?edit` page is for) and the prefix it serves the bundle
under (`__ASSETS_PREFIX__`). Everything else goes over dufs's HTTP API, which
`frontend/src/models/dufs.ts` wraps: `?json` lists a folder, `?q=` searches
under it, `?zip` archives it, PUT writes, MKCOL makes a folder, MOVE renames,
DELETE removes. dufs serves its assets directory flat and reads `index.html`
once at startup, so the bundle keeps fixed names (`index.js`, `index.css`) and
the page asks for them with a per-build `?v=` stamp.

A folder window has a sidebar, a toolbar (the path, the view switcher, the +
button, and the view options), and the folder. The sidebar lists Shortcuts
(Workspace and Uploads, each shown when it exists) and Connected: every file
and folder shared from the user's computers. A share's icon wears a badge in
its corner for how it reaches the workspace (a chain: linked, read live from
the computer; circular arrows: synced, a copy kept here), and its own entry
wears the same badge wherever a folder view lists it. After the label come
marks only when there is something to say: a "Read-only" word badge, a filled
orange disc when a synced copy has not caught up, and a struck-through laptop
when its computer is not answering. The row's tooltip says, without repeating
the name, how it reaches the workspace and how it stands. A synced share opens
at its copy, the others live. The list ends in "Connect a file or folder",
which opens a dialog for the path, the access, and whether to keep a synced
copy, which asks the computer; the request then waits in a row of its own until
it is approved. Something dragged in from the computer onto the Connected
section opens the same dialog, filled with its name (a browser tells a page
nothing of where a dropped item lives, so the path is a guess the user, or
Imbue Studio's own dialog, corrects); dropped on a share's row it uploads into
that share instead, as onto any folder. The sidebar's width is dragged from
its edge (double-click puts it back) and kept with the other preferences. Jumping to a sidebar row starts the
column view afresh there; drilling down into a folder that happens to be a
shortcut keeps the columns already open. There are no back and forward
buttons (Cmd/Ctrl+[ and ] still move through the history), and the model can
still search a folder (dufs's `?q=`), but the toolbar offers no search field
for now.

The folder is laid out as icons (image files as thumbnails), a list with
sortable columns, or columns that open to the right as folders are chosen and
end in a preview of the file chosen. The layout, the sort order, and whether
"system files" (dotted names) show are kept in the browser's localStorage and
shared by every window.

- Click selects, Cmd/Ctrl-click toggles, Shift-click extends; a double-click (or
  Enter) opens. F2 renames in place, Delete deletes after a confirmation,
  Cmd/Ctrl+A selects everything, Cmd/Alt+Up goes up a folder, Cmd+[ and Cmd+]
  go back and forward, and typing a name jumps to it.
- Right-click on an entry offers Open, Open in New Window (a folder), Edit (a
  text file), Download (a folder as a zip), Rename, Copy Path, and Delete,
  ahead of the workspace's own element-menu rows; right-click on a folder's
  background offers New Folder, New File, Upload Files, and Upload Folder,
  which the + button offers too.
- Files dragged in from outside upload into the folder under the pointer (a
  folder's tile or row, a column, a shortcut, a path segment) or else the folder
  on show, folders included; entries dragged onto a folder move into it. An
  upload whose name is taken asks whether to replace it or keep both. Uploads
  show their progress in a panel at the bottom right, which goes away a moment
  after the last one lands and stays up when one failed.

Under 700px of the frame's own width the window lays itself out for a phone:
the sidebar floats over the folder from a button at the toolbar's start, there
is no column view, one tap opens, and every
entry carries a "..." button with its menu.

## Opening a folder or a file

The manifest's one launch path is `/home/user/workspace/` with an optional `path`
param (an absolute path, since dufs serves the filesystem root), so a plain launch
opens the workspace folder and the shell opens a Files window at
`/home/user/workspace/?path=<folder>` when asked for one (`workspace-layout open
files --path /home/user/workspace/data/notes/` is the same thing). The page shows that
folder in place and reports it as its location (`shell:location`,
desktop-interface contracts.md section 7), as it does after every move, so the
window's stored path follows, a reload reopens the folder, and the window is
titled after it.

Framed by the shell, opening a file opens its `?view` page in a Files window of
its own, and Edit its `?edit` page, each through the app contract's `shell:open`
with `ifPresent: "focus"`, so a window already on that page is raised instead of
a second one opening. Outside the workspace the page opens in place. A file's
window is for that file alone and is closed when done with: it offers no way
back to the folder, and deleting the file leaves it saying so. A view page draws images, video, audio, and PDFs in the browser's own
viewers and text as text; an edit page is a text editor that saves with Save or
Cmd/Ctrl+S. Both offer Download, Rename, Copy Path, and Delete, and a view page
of text an Edit icon beside Download.

`frontend/public/404.html` is Files' own "not found" page, copied into the
build as-is: dufs serves a `404.html` found in its `--assets` directory, with
status 404, for every path that does not exist (it answers a client it takes
for a script, such as curl, with a bare "Not Found" instead). dufs fills in no
placeholders there, so the page reads the path asked for from its own address,
names it, links the nearest folder above it that exists (asking each folder's
`?json` listing, whose `dir_exists` says whether it is there), and posts the
location beacon, titled after the missing name.

When `install_dufs.sh` bumps the pinned dufs version, check the placeholders,
the listing JSON, and the verbs `models/dufs.ts` relies on against the new
release.

## Opening a linked file or folder

The manifest also registers the `open:file` message (`[[message_handlers]]`,
desktop-interface contracts.md section 5.6), which the shell sends when a link
to a file or folder is clicked in any app (a `file:` URL the app contract hands
it, such as a chat's link to an absolute path). The shell shows the path's
`?view` page in a Files window: a file's view page, or, for a folder, the folder
itself, since Files reads `?view` on a folder as the folder. A window already on
that file or folder, as `/a/b`, `/a/b/`, or `/a/b/?view`, is raised instead of
opening another.

## Tests

`system/test_app_manifests.py` checks the manifest against the program line, and
`system/test_supervisord_layout.py` the program block. `src/files_app/server_test.py`
drives the Files server against an in-memory dufs and gateway (`testing.py`'s
`FakeComputer` answers as a computer's file server does). The frontend's own
vitest suites sit beside its sources (`npm test` at `system/`).
`test_files_app.py` (marked `browser`) serves the build from a stand-in for
dufs (`conftest.py`, over a temporary folder) and drives the app in Chromium:
the three views, selection, rename, delete, new folders, uploads by picker and
by drop, the sidebar's rows and resizing, the phone layout, and the file
pages. The shell's
`test_e2e.py` runs the real dufs, where installed, to check that files open in
workspace windows.
