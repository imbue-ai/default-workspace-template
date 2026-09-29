- mngr is pinned to the paired `mngr/small-fixes-sep27` change. It rebuilds the ttyd web client so a workspace's web terminal fits itself to its frame whenever it reconnects; before, a terminal resized while its connection was down came back at its old width, which truncated the chat's source view after its window was resized.

- The desktop-interface plan now describes the chat root's filled selection: with nothing selected it shows the most recent chat, and with no chats one awaiting its first send.
