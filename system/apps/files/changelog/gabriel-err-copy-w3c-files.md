Files tells a failed request the way the rest of the workspace does: a sentence of ours, and what was tried, the status and the server's own text behind "See details".

- A refused create, move, rename, delete or save names what failed, then why: "Couldn't move "notes.txt" into "reports". That folder is read-only.", "Couldn't create "untitled folder". This folder is read-only.", "Couldn't save "notes.txt". The folder it's in is read-only.", or the server's own cause ("Something with that name is already there."). Before, a drag into a read-only folder read "You don't have permission to change or read .../notes.txt.", naming the dragged file. One with no explanation names what failed and says to try again ("Couldn't move "b.txt" into "zeta". Try again.").

- Every failure that leaves you unable to do what you meant, whether or not its cause is known (a read-only folder, a name already taken, no permission, a computer that shares read-only), stays until dismissed with "See details" and "Ask an agent to fix this" inside the workspace's desktop. The ask goes to the chat about Files, opened beside the window (the main chat where there is none to take it), and tells the agent what you were doing and where, with the full paths, and to do it if it can or else say why it can't be done and what you can do instead. "Report a bug" takes the agent's place only where none can be asked. A failure whose whole remedy is to try again (nothing answered, something in use, a name too long, a computer that isn't answering) is a note that goes on its own, its details behind "See details", which keeps it up once opened.

- A full disk stays with "Ask an agent to free up space", which asks in the main chat with freeing up space as the agent's task. The button reads "Asking an agent…" until the chat opens and leaves its toast up; when the ask could not be sent, a note says why and offers "Draft it in a chat" and "Report a bug".

- An item that is already gone is no longer told as a refusal. Deleting it is not a failure: the folder is read again and it disappears. Moving, renaming, saving or downloading it says ""todo.md" isn't there anymore. It may have been moved or deleted.", and the folder it was in is read again. This holds for files shared from your computer too.

- A failed rename says why in the name field and reads the folder again; one nothing explained ends the rename and leaves a toast that stays.

- A folder that cannot be listed, and a failed read of what your computers share, say so in the same words, with the details behind "See details", instead of the server's text. When the failure owes help, each offers "Ask an agent to fix this" (beside Try again for a folder; the agent is told which folder); if the ask can't go through, the reason shows under the buttons with "Draft it in a chat", and one "Report a bug" files it with the failure.

- A request nothing answered says the workspace didn't answer, and for a change that it may not have gone through, rather than the server's or a proxy's text.

- A file whose text cannot be loaded says so in place of the editor ("This file is no longer there. It may have been moved or deleted." for one that is gone), with Try again, the details behind "See details" and, when the failure owes help, the same "Ask an agent to fix this".

- A failed upload's row says why: interrupted, a read-only folder, out of disk space, or failed, with the sentence and the details on hover.

- A file whose location is too long to open in a window of its own says to download it, instead of the desktop refusing the window.

- The Files server answers a refusal as JSON: a `user_facing_message` naming only the cause, a `kind`, `detail`, and `expected: true` where the refusal is ordinary operation its sentence fully explains (in use, a name too long, a computer that isn't answering). A disk error is classified by its errno: a read-only file system, a full disk (507), something gone, already there, a name too long, in use and the like each get their own sentence; a move refused for want of write access says which folder refused. A change your computer refuses says it only shares that to read, that it's no longer there, or that your computer couldn't do it, with the computer's status and text behind "See details"; a read or a listing of a path your computer says is gone is answered as gone rather than as the computer not answering. A refusal to a download, or to a page the browser loads, stays plain text.
