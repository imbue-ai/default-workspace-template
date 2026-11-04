The desktop delivers messages that apps send each other, and opens the links apps and Imbue Studio hand it.

- An app's page can send the desktop a message (`shell:message`) for whichever app registered its type. A handler that shows a page has the desktop build the page from the message and raise a window already on it, or open one; a message from a pulled-out window lands on the main desktop, and when the main Imbue Studio window is closed a notice says to reopen it.

- When a message cannot be delivered, a notice says why in the words of the app that refused it (for example, that Chromium is not installed yet).

- A link an app's page hands over (`shell:open-link`, which the app contract sends for every link that is not the page's own), and a popup to a local address that Imbue Studio now turns back into the workspace, open where they belong: a `file:` URL in the File Viewer (`open:file`), a local URL on an app's `<label>.localhost` host or at the port it registered (`http://localhost:8095/...`) as that app's window at the URL's path, any other local URL in the browser (`open:url`), and one of this workspace's app addresses as that app's window; another workspace's address is refused with a notice. A link to an app's window the user popped out into its own window, a link to another page of the page's own app opened in a new window included, raises that window instead of pulling it back onto the desktop. A link opened from a pulled-out window while the main Imbue Studio window is closed lands on the desktop, with a notice to reopen that window (the windows route's answer says so as `has_no_desktop_window`).

- A link in the desktop's own chrome opens the same way a forwarded popup does.

- A port-less link to `<label>.localhost` (or `<name>.localhost`) opens that app's window at the link's path, the way an agent links an app of a workspace never shared; a `localhost:<port>` link at an app's registered port still does too.

- An address on the domain the workspace was last shared under (`https://<label>.<share domain>/...`) opens as that app's window, in the desktop app and in a shared browser alike, so a share link an agent writes or a user pastes stays inside the workspace.

- An external web, mail, or phone link goes to the app registered for `open:web`, `open:mailto` or `open:tel`; with none, it goes back to Imbue Studio (`minds:open-external`), or opens in a new browser tab when no Imbue Studio chrome frames the desktop.

- The inventory lists each app's `share_url`, its address on the domain the workspace was last shared under, which the share gateway now keeps past an unshare; a first share reaches open windows within the inventory's next sweep.
