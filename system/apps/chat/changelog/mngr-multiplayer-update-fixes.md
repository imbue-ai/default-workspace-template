- A sender's name in a chat no longer stays stuck on what the page knew when it loaded. When a message arrives naming its author differently than the page's copy of the roster, or naming someone the copy lacks, the page reads the roster again, and an already-drawn message picks up the new name.

- A person nothing names is called "Guest" instead of their account's raw id, both when the chat app names a sender and when the page draws one.

- A failed read of the shell's roster is now logged as a warning (once per run of failures) instead of only at debug level, so a chat that cannot see who is in the workspace is visible in the logs.
