Files tells a failed request the way the rest of the workspace does: a sentence of ours, and the status and the server's own text behind "See details".

- A refused create, move, rename, delete or save names what failed, then why: "Couldn't move "notes.txt" into "reports". That folder is read-only.", "Couldn't create "untitled folder". This folder is read-only.", "Couldn't save "notes.txt". The folder it's in is read-only.", or the server's own cause ("Something with that name is already there."). Before, a drag into a read-only folder read "You don't have permission to change or read .../notes.txt.", naming the dragged file. One with no explanation names what failed and asks for a bug report ("Couldn't move "b.txt" into "zeta". Try again. If this persists, report a bug."). One that asks for a bug report stays until dismissed, with "See details" and "Report a bug" where Imbue Studio can open its bug report (decided when the toast is drawn, and the report carries the toast's sentence and details); a full disk stays with "Ask an agent to free up space"; any other is a note that goes on its own. A failed rename says why in the name field.

- A folder that cannot be listed, and a failed read of what your computers share, say so in the same words, with the details behind "See details", instead of the server's text. A folder listing that asks for a bug report offers "Report a bug" beside Try again where Imbue Studio can open one.

- A request nothing answered says the workspace didn't answer, and for a change that Imbue Studio can't tell whether it went through, rather than the server's or a proxy's text.

- A file whose text cannot be loaded says so in place of the editor ("This file is no longer there. It may have been moved or deleted." for one that is gone), with Try again and the details behind "See details".

- A failed upload's row says why: interrupted, a read-only folder, out of disk space, or failed, with the sentence and the details on hover.

- A file whose location is too long to open in a window of its own says to download it, instead of the desktop refusing the window.

- The Files server answers a refusal as JSON: a `user_facing_message` naming only the cause, a `kind`, and `detail`. A disk error is classified by its errno: a read-only file system, a full disk (507), something gone, already there, a name too long, in use and the like each get their own sentence instead of asking for a bug report; a move refused for want of write access says which folder refused. A refusal to a download stays plain text, and reads "Couldn't download notes.txt: It's no longer there." for a file that is gone.
