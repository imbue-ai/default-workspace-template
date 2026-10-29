`docs/specs/chat-send-failure-system.md` covers mngr's `not_submitted` send-failure kind: the chat app sends a message again once on its own after it, to the same place, where nothing was delivered, and the notice reads it as `unknown`.

mngr is pinned to the mngr-internal commit that adds `not_submitted` and reads Claude's screen for a send it has waited out, so the Dockerfile's dependency and build steps and the create templates' build arguments pass the build the credential it fetches that pin with.
