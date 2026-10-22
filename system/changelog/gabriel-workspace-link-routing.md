Links clicked inside a workspace open inside the workspace: files in the File Viewer, local addresses as the window of the app on whose `<label>.localhost` host or registered port they are, or else in the workspace's browser, an address on the workspace's share domain as that app's window, and external links in an app of the workspace registered for them, or else outside the workspace.

- The File Viewer registers `open:file`, shown on the file's (or folder's) view page and raising a window already there.

- AGENTS.md tells agents to link files by absolute path, a registered app by the `link` `workspace-layout list` prints for it (its share address once the workspace has been shared, else its port-less `<label>.localhost` address), and other services running in the workspace by their localhost URL.

- The desktop-interface contract describes each app's `share_url` in the inventory, the recorded share domain, and the `open:web`, `open:mailto` and `open:tel` messages external links become.

- The desktop-interface and workspace-app-model contracts describe the new message handler forms, `shell:message`, `shell:open-link`, the app contract's link rule, and `minds:open-link`, and drop the reserved `handles` table.

- `fetch_mngr_assets.sh` also fetches the desktop app's URL externality vectors, which the link classifier is tested against, and the template pins mngr to the export carrying embed contract version 8.
