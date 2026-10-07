Links in chat open inside the workspace instead of downloading or doing nothing.

- A link to an absolute path opens the file or folder in a File Viewer window (raising one already showing it) instead of downloading it.

- A link to `localhost`, `127.0.0.1`, `[::1]`, or a `*.localhost` address is clickable again. A `localhost`, `127.0.0.1`, or `[::1]` link at the port of an app registered with the workspace opens as that app's own window (so an agent can link an app it runs by its localhost URL); any other opens in the workspace's browser.

- External web, mail, and phone links still open in your own browser. All links look the same, and hovering or copying one shows its real address; Cmd/Ctrl-click and middle-click do what a plain click does, and the chat itself never navigates away.

- Opened on its own outside the workspace, the chat downloads a linked file and opens a local link in a new tab.
