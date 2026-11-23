The desktop-interface contracts (`docs/system/blueprint/desktop-interface/contracts.md`, section 7) list `firstConversationStep(isPending)` among what `connectToShell` returns, and the comment on `shell:show-coach-card` in `workspace_ui`'s app contract says when the shell shows a card as the shell does it: while its tour shows nothing, in place of a coach card that is up, and in place of the tour's last card or exit badge.

`uv.lock` lists the Onboarding checklist app among the chat app's dev dependencies.
