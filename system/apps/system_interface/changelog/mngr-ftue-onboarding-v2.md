A first-run workspace opens with the chat app's "Your own cloud computer, any AI" page covering the whole desktop, for the workspace's owner, until they connect an AI provider or skip. The show-around tour waits until the cover is down.

The shell stops its page layer's focus watcher when its view is removed, so the watcher's timer no longer goes on ticking against a document that is gone (the cause of a `document is not defined` error at the end of the shell's frontend test run).
