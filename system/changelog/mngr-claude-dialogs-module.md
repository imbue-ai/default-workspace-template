Claude agents now deal with a dialog that is holding the TUI's input at send time, instead of failing the send.

`sensibly_deal_with_dialogs` lists the confirmations mngr may answer on the user's behalf. It is set to the model bar's own two: picking a model or an effort level sends `/model <slug>` or `/effort <level>`, and claude then asks to confirm that switching mid-session re-reads the whole history. The user already made that choice in the picker, so mngr completes it — by cycling the selector onto the named option, never by pressing Enter on whatever happens to be highlighted. Anything else that needs a real answer refuses the send with an error naming what is in the way; anything Esc closes harmlessly is dismissed regardless of the list.

`auto_dismiss_dialogs` is renamed `auto_dismiss_dialogs_at_startup` for claude, codex and pi-coding. Same behaviour; the name now says when it applies, which matters more now that a second dialog setting exists.

Claude also runs with `tui = "fullscreen"`, which pins the input bar and any dialog to the pane rather than letting them scroll out of view — so what mngr captures at send time is what is actually holding the input. tmux here runs with `alternate-screen off`, so the workspace terminal panel keeps its scrollback either way.
