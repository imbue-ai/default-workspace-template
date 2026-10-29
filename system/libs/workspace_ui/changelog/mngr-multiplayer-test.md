- The app contract reports input: every page that binds it sends `shell:input` (a pointer press or a key, throttled to one message per 20 seconds) up to the shell, whose presence heartbeat reads the client's activity off it -- input inside an app's frame never bubbles to the shell's own window, which is why everyone used to read as idle.

- New colour tokens: the viewer's own chat bubble becomes a green tint (`--c-bg-user-bubble`), a bubble that is not yours (another person's or the agent's) and the chat list's selected row share one 5% grey (`--c-bubble-other`), and `--c-fill-faint` (3%) is a hover one step lighter than a selection drawn with it.

- The shell's handshake carries `isViewing`: whether the window is being looked at on somebody else's desktop (the multiplayer spec, section 8.3), sent again when that changes. A page attaching to something shared (a terminal's tmux session) attaches as a viewer then.

- The icon set gains `mail`, an envelope, which the desktop's tray draws for an invited email, and `user`, a person's silhouette, which it draws for the owner of a workspace that has not shared (the mngr repo's `specs/multiplayer-refinements/spec.md`, sections 6.2 and 6.3).
