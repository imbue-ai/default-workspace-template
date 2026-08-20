Recorded the implementation plan for the chat transcript's scroll virtualization rewrite under `docs/system/blueprint/chat-scroll-virtualization-rewrite/`.

It sets out why the transcript jumped while reading history -- scroll space for unloaded history was reserved per transcript event while the renderer groups a whole turn into one row -- and the plan that replaced the guess with measured row geometry, alongside the earlier plans it follows on from.
