A page's "Ask an agent to fix this" (`shell:start-new-with-text`) starts a new chat: the shell runs the launcher's secondary text action (Chat's New Chat), or the primary one where there is no second, instead of posting the task into the main chat.

- The shell answers the page with `shell:start-new-with-text-result` once the chat's window opened, or with the failure; the shell toasts that failure too unless the request says `showsOutcome` (an "Ask an agent" button showing it where it was pressed), the fallback sentence if the launch threw.

- Every handshake carries a `shellId` made once per load of the shell, so a page tells a restarted shell from a re-sent greeting.

- A page's `shell:start-with-text` is answered the same way, with `shell:start-with-text-result`, and the store's `startWithText` and `startNewWithText` answer their outcome without a toast, for a caller that shows it.
