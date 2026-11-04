- The update notice's list of apps can now include an ordinary app: a careful pass that also changed one keeps it in the rollback record, so the banner names it and a rollback restarts it.

- The tests' WebSocket client now also delivers messages that arrive in the same socket read as the handshake response. Before, a test of the `/api/ws` replay could time out when the server's first messages arrived together with the accept.
