A page's "Ask an agent to fix this" (`shell:ask-agent`) is sent to the chat about the app it was pressed in, which opens beside the window as "Chat about this app" does.

- The shell runs the first launch path whose `app_params` names a `text` param (Chat's `app-ask`) with the window's app, its display name and the task. From an internal app's window, or where no app declares such a path, it runs the launcher's primary text action (the main chat). "Chat about this app" runs only a launch path whose `app_params` names no `text`.

- The shell answers the page with `shell:ask-agent-result` once the chat's window opened, or with the failure; the shell toasts that failure too unless the request says `showsOutcome` (an "Ask an agent" button showing it where it was pressed), the fallback sentence if the launch threw.

- Every handshake carries a `shellId` made once per load of the shell, so a page tells a restarted shell from a re-sent greeting.

- A page's `shell:start-with-text` is answered the same way, with `shell:start-with-text-result`, and the store's `startWithText` and `askAgent` answer their outcome without a toast, for a caller that shows it.
