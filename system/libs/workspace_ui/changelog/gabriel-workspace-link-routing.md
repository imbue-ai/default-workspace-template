Links clicked inside the workspace open inside the workspace.

- New `links.ts`: one classifier (`classifyLink`) says whether a link is external, a file path, a local URL, one of this workspace's app addresses, another workspace's, or unroutable; `routeLink` acts on it (`open:file` or `open:url` through the shell, the page's own window, or the browser), falling back to a download or a new tab when no shell frames the page; `installLinkRouting` routes plain, modified, and middle clicks alike. A test holds the classifier to calling external exactly the URLs the Imbue Studio desktop app does.

- The app contract's `connectToShell` gains `sendMessage(type, fields)`, which sends the shell `shell:message`, and `openLink(url)`, which asks the shell to open another app's address as that app's window (`shell:open-link`).

- The element menu's "Open link in new window" is now "Open link", and opens the link the way a click would.

- The embed module knows `minds:open-link` and announces `opensLinks` with `minds:workspace-ready`.
