Bug reports keep more of what they collect when the secret scan finds something.

- The scan now withholds only the files it flags, not the whole group. Before, one chat with a flagged line withheld every chat, and one flagged service log withheld every service log. `collection-notes.txt` names each withheld file, so a partial set never reads as the complete one. A file whose own name was flagged is listed without its name.

- Credentials in URL query strings are blanked before the scan. The value of a parameter like `token=`, `access_token=`, `api_key=` or a presigned link's `X-Amz-Signature=` becomes `REDACTED`, so a newsletter sign-in link in a chat, or a token-carrying URL in a log, no longer costs the report that file. The scan still reads the result, so anything the blanking misses is withheld as before.
