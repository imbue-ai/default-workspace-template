Add diagnostic logging for the chat frontend/backend desync investigation (frozen transcripts, stale activity states, and layout ops failing with "no client" 412s):

- Backend: log every /api/ws connection open/close with a disconnect reason, every client_state registration and layout switch, and every SSE stream open/close with a close reason. The 412 rejection for layout ops now logs (and includes in its error detail) the server's current connected-client registry, so an agent seeing the error can tell exactly what the server thinks is connected.

- Frontend: console logging (prefixes [si-ws] and [si-sse]) for WebSocket connect/open/close (with close code and reason)/error and reconnect scheduling, client_state sends and skips, and SSE stream open/error/reconnect/explicit-disconnect plus snapshot-refetch failures during reconnect.
