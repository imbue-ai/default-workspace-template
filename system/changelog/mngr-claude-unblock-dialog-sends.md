Sends to a claude agent no longer fail outright when a dialog is holding the TUI's input.

`auto_accept_preflight_prompt_depth` and `auto_accept_prompt_depth` both default to `0` — "press Enter zero times" — so mngr was detecting a blocking dialog and then doing nothing about it, reporting the send as failed. Neither was set for `[agent_types.claude]`. Both are now `2`.

Two, not one, because the `/model` flow is two dialogs deep: the picker, then a confirmation that switching mid-session invalidates the prompt cache. A depth of `1` clears the picker and leaves the confirmation holding the input. The two knobs cover different moments — preflight is a dialog already up when the send starts, post-submit is one the message itself opened, which is the `/model` case since the picker renders after delivery.

Also added to the managed settings overlay: `tui = "fullscreen"`, which pins the input bar and any dialog to the pane instead of letting them scroll away, and `autoUpdates = false`, which stops claude offering to change its update channel when the version is pinned by the image anyway.

This accepts whichever option a dialog has highlighted, which is blunt — on a dialog whose default is not the answer you want, it takes the wrong one. It is the mechanism mngr ships today and is strictly better than doing nothing. The replacement, a registry that recognises each dialog and cycles onto a named option before pressing Enter, is `mngr_claude.dialogs` (imbue-ai/mngr-internal#499) and lands behind this.
