A page's "Ask an agent to fix this" (`shell:ask-agent`) is sent to the chat about the app it was pressed in, which opens beside the window as "Chat about this app" does.

- The shell writes the message the agent reads, for every ask: `/assist`, then the app and window the ask was pressed in ("This is about the Files app (`files`, window `<id>`)."), then the page's task, so the agent runs the workspace's assist flow and knows where the failure was shown. It runs the first launch path that declares `app_params` and a `text_param` (Chat's `app-chat`, the path "Chat about this app" runs) with the window's app and its display name, and that message in the `text_param`. From an internal app's window, where no app declares such a path, or for a page that asks for the main chat (`delivery: "main-chat"`), it runs the launcher's primary text action with it. `delivery: "draft"` leaves it unsent in a chat's message box, which a refused ask offers.

- The shell answers the page with `shell:ask-agent-result` once the chat's window opened, or with the failure; the shell toasts that failure too unless the request says `showsOutcome` (an "Ask an agent" button showing it where it was pressed), the fallback sentence if the launch threw.

- Every handshake carries a `shellId` made once per load of the shell, so a page tells a restarted shell from a re-sent greeting.

- A page's `shell:start-with-text` is answered the same way, with `shell:start-with-text-result`, and the store's `startWithText` and `askAgent` answer their outcome without a toast, for a caller that shows it.
