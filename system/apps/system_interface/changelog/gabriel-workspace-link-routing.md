The desktop delivers messages that apps send each other, and opens the links Imbue Studio hands it.

- An app's page can send the desktop a message (`shell:message`) for whichever app registered its type: a chat's file link becomes `open:file`, which the File Viewer shows as a window, and its local link `open:url`, which the browser opens. A handler that shows a page has the desktop build the page from the message and raise a window already on it, or open one; a message from a pulled-out window lands on the main desktop.

- When a message cannot be delivered, a notice says why in the words of the app that refused it (for example, that Chromium is not installed yet).

- Popups a workspace page opens to a local address, which Imbue Studio now turns back into the workspace, open where they belong: a local page in the browser, one of this workspace's app addresses as that app's window, and another workspace's address is refused with a notice.
