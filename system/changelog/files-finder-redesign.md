The File Viewer is now Files, rebuilt to feel like Finder or the Windows Files app instead of a directory listing.

- A resizable sidebar with Shortcuts (Workspace, Uploads) and Connected: every file and folder shared from your computers. Each one's icon carries a small badge saying whether it is linked (opened live from your computer) or synced (a copy kept here), and the same badge marks it wherever a folder lists it. After the name, a "Read-only" badge, an orange alert when a synced copy hasn't caught up, or a crossed-out laptop when your computer isn't answering; hovering says how it syncs and how it stands. "Connect a file or folder" at the end of the list, or dragging something from your computer onto Connected, asks your computer to share it through Imbue Studio's sharing dialog, and the request waits in its own row until you approve it. Dropping onto a shared folder's row uploads into it instead. A toolbar with the path, a view switcher, a + button, and view options.

- Files has a small server of its own in front of dufs, which reads the shares through the latchkey gateway, opens unsynced ones live from your computer, and keeps file names with unusual characters (like the narrow space in macOS screenshot names) from breaking their preview.

- Three views: icons (images as thumbnails), a list with Name, Date Modified, Size, and Kind columns that sort, and columns that open to the right as folders are chosen and end in a preview of the chosen file. The view, the sort order, and whether system files show are remembered.

- Click to select (Cmd/Ctrl and Shift to select several), double-click or Enter to open, F2 to rename in place, Delete to delete after a confirmation, and keyboard arrows, Cmd/Ctrl+A, Cmd/Alt+Up, and type-to-find.

- Right-click an item for Open, Open in New Window, Edit, Download (folders as a zip), Rename, Copy Path, and Delete; right-click empty space, or use +, for New Folder, New File, Upload Files, and Upload Folder.

- Drag files or folders in from your computer to upload them into the folder under the pointer, with progress shown as they go (the panel tidies itself away once they are done); a name already there asks whether to replace it or keep both. Drag items onto a folder to move them.

- A file opens in a window of its own, to be closed when you are done: it previews pictures, video, audio, and PDFs, shows text, and edits it (the Edit icon beside Download) with Save or Cmd/Ctrl+S; it also offers Rename, Copy Path, and Delete.

- On a phone-sized window the sidebar floats over the folder from a toolbar button, the column view steps aside, a tap opens, and each item has a "..." menu.

- The Files frontend is a member of the npm workspace (`system/apps/files/frontend`), built into the gitignored `system/apps/files/static/` that dufs serves; the image installs its package with the others, and `install_dufs.sh` now says what a dufs bump must be checked against.
