`matchesQuery` (every whitespace token of a query occurring in one of the given texts, case-insensitively) moves from the shell into the library as `src/search.ts`, so the launcher's rows and the Getting Started page's search filter the same way.

The app contract gains `shell:start-with-text`: `connectToShell` returns `startWithText(text)`, which asks the shell to run the launcher's primary free-text row with the text (start a chat with it, on a machine whose chat declares one), so a framed page seeds a chat without naming the chat app.
