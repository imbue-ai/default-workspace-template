Files tells a failed request the way the rest of the workspace does: a sentence of ours, and the status and the server's own text behind "See details".

- A refused create, move, rename, delete or save shows the server's own sentence where it wrote one ("There's already something at ...", "You don't have permission to change or read ..."), else names what failed ("Couldn't move "b.txt" into "zeta". Try again. If this persists, report a bug."). One that asks for a bug report stays until dismissed, with "See details" and "Report a bug" where Imbue Studio can open its bug report; any other is a note that goes on its own. A failed rename says why in the name field.

- A request nothing answered says the workspace didn't answer, and for a change that Imbue Studio can't tell whether it went through, rather than the server's or a proxy's text.

- A file whose text cannot be loaded says so in place of the editor ("This file is no longer there. It may have been moved or deleted." for one that is gone), with Try again and the details behind "See details".

- A failed upload's row says why: interrupted, a read-only folder, or failed, with the sentence and the details on hover.

- A file whose location is too long to open in a window of its own says to download it, instead of the desktop refusing the window.

- The Files server answers a refusal as JSON (`detail`, and `user_facing_message` when its words are written for the person); a refusal to a download stays plain text.
