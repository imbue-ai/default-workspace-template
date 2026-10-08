Links clicked inside a workspace open inside the workspace: files in the File Viewer, local addresses as the window of the app registered at their port or else in the workspace's browser.

- The File Viewer registers `open:file`, shown on the file's (or folder's) view page and raising a window already there.

- AGENTS.md tells agents to link files by absolute path and services running in the workspace by their localhost URL, which opens a registered app as its own window.

- The desktop-interface and workspace-app-model contracts describe the new message handler forms, `shell:message`, `shell:open-link`, the app contract's link rule, and `minds:open-link`, and drop the reserved `handles` table.

- `fetch_mngr_assets.sh` also fetches the desktop app's URL externality vectors, which the link classifier is tested against, and the template pins mngr to the export carrying embed contract version 8.
