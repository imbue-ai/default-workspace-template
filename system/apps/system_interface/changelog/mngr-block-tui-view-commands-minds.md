Chat now declines the slash commands that would leave an agent unable to receive messages, instead
of sending them.

A chat message reaches an agent by being typed into its terminal, so a command that changes the
terminal rather than starting a turn does something chat cannot undo -- most of these replace the
input box with a full-screen view, and `/exit` shuts the session down. The composer now declines
them with a short notice pointing at the agent's terminal, where they still work, and keeps the
typed message.

Declined: `/add-dir`, `/config` (`/settings`), `/diff`, `/exit` (`/quit`), `/extra-usage`, `/goal`,
`/help`, `/hooks`, `/ide`, `/mcp`, `/permissions` (`/allowed-tools`), `/powerup`,
`/privacy-settings`, `/release-notes`, `/skills`, `/status`, `/tasks` (`/bashes`), `/theme`,
`/usage` (`/cost`, `/stats`), `/usage-credits`, `/workflows`.

Each entry was measured against a live agent rather than inferred, and commands that turned out to
send fine are deliberately still allowed -- including `/model`, `/plugin`, `/rewind`, `/export`,
`/version`, `/effort`, `/tui`, `/clear` and `/compact`.
