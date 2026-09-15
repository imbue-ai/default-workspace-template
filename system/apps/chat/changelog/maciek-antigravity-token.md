The Google provider offers "Use a Gemini API key" alongside its two browser sign-ins: paste an AI Studio key and the account is signed in, with nobody at a browser. This is the only way to put a workspace on Antigravity unattended.

The key is checked against Google the moment it is pasted, not merely written: agy lists the same Gemini models for any key at all, valid or not, and answers an invalid one at turn time by opening a sign-in page nobody is there to complete -- so a mistyped key is refused at the field rather than surfacing later as a chat that cannot take a turn.

The account is called "Google Gemini", so it reads apart from the browser accounts on the same provider, and it works like any other: re-auth, deletion, the default pin, and binding a chat to it.

One limit, which the sign-in screen states: a chat already running keeps the key it was created with, because the key rides each chat's own environment rather than a file agy reads at every turn. Re-keying an account reaches chats created afterwards, so a chat on a key that has been replaced has to be started again -- and deleting the account, which removes the row and the folder, leaves those chats running on the key too. Replacing a key that must stop being used means restarting the chats that hold it.

Separately, a re-auth that is interrupted mid-flight now restores an Antigravity credential to where agy reads it. The parked copy the next boot puts back is keyed by the file's path within the account rather than by its name, so a credential that lives in a subdirectory -- both of agy's do -- no longer comes back at the folder's top level, where nothing reads it and the account looks repaired without working.
