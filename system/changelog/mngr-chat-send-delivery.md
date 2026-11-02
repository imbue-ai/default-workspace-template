`docs/specs/chat-send-failure-system.md` covers the send ledger's part in Retry: Retry goes under the failed send's send-time id, so the chat app answers it as it answered that send when the send was taken after all, and a send that got no answer is handled as `submission_unconfirmed`.

`docs/specs/chat-send-failure-system.md` and `docs/specs/send-failure-notice.md` now describe the notice's own sentence for each kind of send failure, the Restart button, a stopped send answering `withdrawn`, and the chat app keeping the sends behind "Sent again".
