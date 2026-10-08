Links clicked inside the workspace open inside the workspace, in every app, with no link code in the app.

- The app contract follows a framed page's link clicks by one rule from the moment `connectToShell` connects: a link to the page's own origin navigates as usual (a middle or modified click, or one naming another browsing context, opens a window of the app instead); an external link opens in a new browsing context, which the Imbue Studio desktop app sends to the user's browser; and any other link (a local URL, another app's or another workspace's address, a `file:` URL) goes to the shell as `shell:open-link`. A click the page already handled, a download link, a plain click on a link into one of the page's own iframes, and other schemes are left to the page.

- `connectToShell` also gains `sendMessage(type, fields)`, which sends the shell `shell:message`, and `openLink(url)`, which sends `shell:open-link`.

- New `links.ts`: one classifier (`classifyLink`) says whether a link is external, a file (a `file:` URL, or an absolute path as a chat message writes it), a local URL, one of this workspace's app addresses, another workspace's, or unroutable. The shell and the chat use it, and a test holds it to calling external exactly the URLs the Imbue Studio desktop app does.

- The element menu's "Open link in new window" is now "Open link", and opens the link by clicking it.

- The embed module knows `minds:open-link` and announces `opensLinks` with `minds:workspace-ready`.
