This is the merge of `minh/auth-provider-lanes` (dropping the pool bake's wait for the removed initial-chat sentinel) into current `main`; see that branch's `minh-auth-provider-lanes.md` entry for the bulk of what changed.

On top of the merge, this PR updates `pool_bake.py`'s remaining documentation (module docstring, `finalize_baked_pool_host` step list, and related comments) that still described the removed sentinel wait and chat-agent teardown.

It also trims the change-history narration in `pool_bake.py`'s finalize comment and its unit tests down to the present-tense rationale (why finalize has no chat teardown), per a `/crispy-comments` pass.

Finally, it refreshes `cli/server.py`'s slice-bake documentation that still described the removed teardown: the `_bake_one_slice` docstring now says finalize clears the baked git identity, and the stop-services comment's rationale no longer leans on the removed initial-chat sentinel / boot-created chat.
