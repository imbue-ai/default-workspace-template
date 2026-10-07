The desktop delivers messages that apps send each other, and opens the links apps and Imbue Studio hand it.

- An app's page can send the desktop a message (`shell:message`) for whichever app registered its type. A handler that shows a page has the desktop build the page from the message and raise a window already on it, or open one; a message from a pulled-out window lands on the main desktop.

- When a message cannot be delivered, a notice says why in the words of the app that refused it (for example, that Chromium is not installed yet).

- A link an app's page hands over (`shell:open-link`, which the app contract sends for every link that is neither the page's own nor external), and a popup to a local address or a `file:` URL that Imbue Studio now turns back into the workspace, open where they belong: a `file:` URL in the File Viewer (`open:file`), a local URL at the port an app registered (`http://localhost:8095/...`, the only address of an app an agent knows) as that app's window at the URL's path, any other local URL in the browser (`open:url`), and one of this workspace's app addresses as that app's window; another workspace's address is refused with a notice.

- A link in the desktop's own chrome to a local address opens the same way a forwarded popup does.
