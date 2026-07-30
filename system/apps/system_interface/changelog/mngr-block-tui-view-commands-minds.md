Chat now declines the slash commands that leave an agent unable to receive messages, instead of
sending them.

A chat message reaches an agent by being typed into its terminal, and some Claude Code commands
replace the input box with a full-screen view. Sending one left the agent unable to accept any
further message until someone closed that view from the terminal -- which a chat user cannot do.
The composer now shows a short notice explaining why, and keeps the typed message so nothing is
lost.

Declined: `/add-dir`, `/config` (`/settings`), `/diff`, `/extra-usage`, `/goal`, `/help`, `/hooks`,
`/ide`, `/mcp`, `/permissions` (`/allowed-tools`), `/powerup`, `/privacy-settings`,
`/release-notes`, `/skills`, `/status`, `/tasks` (`/bashes`), `/usage` (`/cost`, `/stats`),
`/usage-credits`, `/workflows`.

Each one was measured against a live agent, and commands that turned out to send fine are
deliberately not declined -- including `/model`, `/plugin`, `/theme`, `/rewind`, `/export`,
`/version`, `/clear` and `/compact`.
