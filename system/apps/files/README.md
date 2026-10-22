# files

Files: the workspace's file browser, the `files` app of the desktop's launcher,
supervised as the `files` program declared in
`system/supervisord.conf.d/files.conf`, which runs the `files-app` entry point of
this package (`src/files_app`). It serves the Files server on the app's port 8300
and registers `app.toml`.

The server serves the container's filesystem root, so a window opened at the
workspace can walk up to anything the workspace user can read; it is bound to
loopback with all operations enabled (browse, preview, upload, rename, delete):
the workspace origin is what gates access, exactly as for every other registered
app.

## The Files server

`server.py` answers a path the same way wherever its files are. `/<path>` is the
workspace's own file or folder (`local_files.py`), and `/__remote/<device id>/<path>`
one shared from the user's computer and read live from it (`device_files.py`);
both offer the verbs `sources.py` names. For either:

- a folder, or a file's `?view` / `?edit` page, is the frontend's `index.html`
  with the path's page data filled in (base64 JSON in `#index-data`: a folder's
  entries, or whether the file is small and reads as text), and `?json` is that
  data alone;
- a file's plain GET is its bytes (a `Range` honoured), `?download` the same
  bytes saved under the file's name, and `?zip` a folder as a zip, written as
  it is sent (linked folders are not followed, and what can't be read is left
  out);
- PUT writes a file (creating the folders above it, writing through a symlink to
  its file, and keeping a written-over file's mode), swapped in whole so an upload
  cut short leaves the old file; MKCOL makes a folder; MOVE renames, never over
  something already there; DELETE removes;
- a path that isn't there is answered with `404.html` when a page was asked for
  (the request accepts `text/html` and is not a `?json`, `?zip`, or `?download`),
  and in plain words otherwise.

A computer's file server has no MOVE, and a folder there is not offered as a zip,
so those are refused, as is a MOVE from the workspace onto a computer. Each name
header a computer sends is made ASCII-safe: a name with a character outside
Latin-1 (the narrow space macOS puts in screenshot names) makes the forwarder
between the workspace and the browser answer with an error in place of the file.

`/__files/assets/` is the frontend's build, and `/__files/api/connections` lists
what the user has shared with the workspace from their computers, read through
the latchkey gateway (`gateway.py`), whose address and credentials every
supervised program inherits. A share is a file-sharing permission the workspace
holds (`minds-file-server-read-<device id>:<path>`, and `-write-` beside it when
changes are allowed; a computer whose Imbue Studio predates per-device grants
names no device id); `connections.py` reads them per device, notes whether Imbue
Studio keeps a synced copy at `~/synced_folders/<device id>/<path>` (two-way with
write access, one-way with read), and, since the workspace learns nothing from
the sync itself, compares the copy's names with the computer's to say when a copy
has fallen behind. Files does not ask for new shares: the user makes them in
Imbue Studio on their computer. A computer that grants per device serves its
files under its id (`/minds-api-proxy/api/v1/files/<device id><path>`, which is
what its grants match); one that predates that serves them with no id
(`/minds-api-proxy/api/v1/files<path>`). Either way the device also goes in the
`X-Latchkey-Device` header, which routes the request to it.

`/__files/api/schema` is the OpenAPI document of the app's routes, built from its
url map (`system/libs/app_schema`); the manifest's `api_schema_path`, which the
visiting-agent primer links. The server's route table says what each route
exchanges: the connections listing answers the `Connections` model and refuses
with `{"detail": ...}`; the file routes serve files of any kind, so they are
declared schemaless with what they answer (`?json` page data included).

## The frontend

`frontend/` is a Mithril app on the shared `system/libs/workspace_ui` library,
one package of the npm workspace at `system/package.json`. Its vite build writes
`static/` (gitignored), which the server serves under `/__files/assets/`: the
files there are named by their content, so a browser keeps them, while
`index.html`, which names this build's, is read afresh for every page. Everything
after the page goes over the server's HTTP API, which
`frontend/src/models/server.ts` wraps.

A folder window has a sidebar, a toolbar (the path, the view switcher, the +
button, and the view options), and the folder. The sidebar lists Shortcuts
(Workspace and Uploads, each shown when it exists) and Connected: every file
and folder shared from the user's computers, each drawn with the folder views'
own folder or page icon and a small badge over its corner for how it reaches
the workspace (a chain: linked, read live from
the computer; circular arrows: synced, a copy kept here), and its own entry
wears the same badge wherever a folder view lists it. After the label come
marks only when there is something to say: a "Read-only" word badge, a filled
orange disc when a synced copy has not caught up, and a struck-through laptop
when its computer is not answering. The row's tooltip says, without repeating
the name, how it reaches the workspace and how it stands. A synced share opens
at its copy, the others live. With nothing shared, the section says to share
from Imbue Studio. Something dragged in from the computer onto a share's row
uploads into that share, as onto any folder. The sidebar's width is dragged from
its edge (double-click puts it back) and kept with the other preferences. Jumping to a sidebar row starts the
column view afresh there; drilling down into a folder that happens to be a
shortcut keeps the columns already open. There are no back and forward
buttons (Cmd/Ctrl+[ and ] still move through the history), and no search.

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
- Download, here and on a file's page, loads the file's `?download` (a folder's
  `?zip`) in a hidden frame (`workspace_ui`'s `startDownload`), so the request
  carries the workspace's session inside Imbue Studio; what the server says
  instead of sending the file shows as a toast.
- Files dragged in from outside upload into the folder under the pointer (a
  folder's tile or row, a column, a shortcut, a path segment) or else the folder
  on show, folders included; entries dragged onto a folder move into it, except
  to or from a folder read live from a computer, which says it can't. An
  upload whose name is taken asks whether to replace it or keep both. Uploads
  show their progress in a panel at the bottom right, which goes away a moment
  after the last one lands and stays up when one failed.

Under 700px of the frame's own width the window lays itself out for a phone:
the sidebar floats over the folder from a button at the toolbar's start, there
is no column view, one tap opens, and every
entry carries a "..." button with its menu.

## Opening a folder or a file

The manifest's one launch path is `/home/user/workspace/` with an optional `path`
param (an absolute path, since Files serves the filesystem root), so a plain launch
opens the workspace folder and the shell opens a Files window at
`/home/user/workspace/?path=<folder>` when asked for one (`workspace-layout open
files --path /home/user/workspace/data/notes/` is the same thing). The page shows that
folder in place and reports it as its location (`shell:location`,
desktop-interface contracts.md section 7), as it does after every move, so the
window's stored path follows, a reload reopens the folder, and the window is
titled after it. A `path` that names a file opens that file's page instead.

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
build as-is and served, with status 404, for every page asked for that does not
exist. Nothing is filled in there, so the page reads the path asked for from its
own address, names it, links the nearest folder above it that exists (asking each
folder's `?json`), and posts the location beacon, titled after the missing name.

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
drives the Files server over a temporary folder and an in-memory gateway
(`testing.py`'s `FakeComputer` answers as a computer's file server does). The
frontend's own vitest suites sit beside its sources (`npm test` at `system/`).
`test_files_app.py` (marked `browser`) runs the Files server over a temporary
folder (`conftest.py`, with the shares stubbed) and drives the app in Chromium:
the three views, selection, rename, delete, new folders, uploads by picker and
by drop, the sidebar's rows and resizing, the phone layout, and the file
pages. The shell's
`test_e2e.py` runs the Files server to check that files open in workspace
windows.
