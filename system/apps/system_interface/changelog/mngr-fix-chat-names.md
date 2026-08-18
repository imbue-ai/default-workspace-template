Chat and browser tabs now follow the same naming scheme minds uses for hosts: a human-readable display name paired with a canonical true name derived from it.

- A chat's display name ("Chat 2") lives on its mngr agent as the `display_name` label, with the canonical form (`Chat-2`) as the agent's mngr name -- so `mngr list`, the agent terminal, and the tab all agree. Display names are minted server-side (the first free "Chat N" / "Codex N" / "Pi N" on the machine, counting agents, in-flight creates, and chosen member titles), so two clients creating at once cannot both mint "Chat 1"; an explicitly requested name that collides answers 409.

- Renaming a chat is back: double-click its tab title or use the tab menu's Rename row. The rename goes through `mngr rename` (label-only via `mngr label` when just the human-readable half changes), keeps the name pair matched, and refuses a name whose canonical form collides with another agent's (409, retry with another name). The member-title store no longer holds chat names; a legacy stored entry is cleared on the first rename so it can never shadow the agent's own name.

- Terminal and browser tabs derive their display names from their identities ("Terminal 3" from the `terminal-3` tmux session, "Browser 1" from the daemon-minted `browser-1` name) instead of filing a second copy into the title store.
