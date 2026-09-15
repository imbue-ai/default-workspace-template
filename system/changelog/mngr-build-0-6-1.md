- Refreshed `system/vendor/mngr` from mngr `0c9d81e7f6508be31c550a165ce48874426f426e`
  (the `minds-v0.6.1` release SHA) via `git archive`, and relocked the root
  `uv.lock` against the vendored libraries. The workspace's in-container mngr
  now matches the 0.6.1 desktop binary: workspace stop kinds, the migrate's
  latchkey-state replay, the chat-agent refactor, proactive compaction, and
  the latchkey 3.13.0 bump among the changes since `minds-v0.6.0`.
