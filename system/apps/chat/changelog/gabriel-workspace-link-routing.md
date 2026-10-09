Links in chat open inside the workspace instead of downloading or doing nothing.

- A link to an absolute path, or a `file:///` URL, opens the file or folder in a File Viewer window (raising one already showing it) instead of downloading it.

- A link to `localhost`, `127.0.0.1`, `[::1]`, or a `*.localhost` address is clickable again. A `localhost`, `127.0.0.1`, or `[::1]` link at the port of an app registered with the workspace opens as that app's own window (so an agent can link an app it runs by its localhost URL); any other opens in the workspace's browser.

- External web, mail, and phone links go to an app of the workspace registered for them, of which there is none yet, and otherwise open in your own browser or mail or phone app (Imbue Studio asks first for mail and phone links). A link to one of the workspace's share addresses opens that app's own window. All links look the same, and hovering or copying one shows its real address; Cmd/Ctrl-click and middle-click do what a plain click does, and the chat itself never navigates away.

- The chat has no link code of its own: clicks follow the app contract's link rule, as in every app. It only renders an absolute path as the file's `file:` URL.

- Opened on its own outside the workspace, the chat opens a web, mail, or phone link itself; a file link there does nothing (it no longer downloads the file).
