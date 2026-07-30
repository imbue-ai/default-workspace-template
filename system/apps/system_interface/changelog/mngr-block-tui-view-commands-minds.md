Chat now declines the slash commands that would leave an agent unable to receive messages, instead
of sending them.

A chat message reaches an agent by being typed into its terminal, so a command that changes the
terminal rather than starting a turn does something chat cannot undo. Two kinds are declined, each
with its own explanation:

- Commands that replace the input box with a full-screen view, leaving the agent unable to accept
  any further message until someone closes that view from its terminal: `/add-dir`, `/config`
  (`/settings`), `/diff`, `/extra-usage`, `/goal`, `/help`, `/hooks`, `/ide`, `/mcp`, `/permissions`
  (`/allowed-tools`), `/powerup`, `/privacy-settings`, `/release-notes`, `/skills`, `/status`,
  `/tasks` (`/bashes`), `/theme`, `/usage` (`/cost`, `/stats`), `/usage-credits`, `/workflows`.

- Commands that end the agent's session outright, after which it stops responding to anything:
  `/exit` (`/quit`).

The composer shows a short notice explaining which of the two applies, and keeps the typed message
so nothing is lost.

Each entry was measured against a live agent rather than inferred, and commands that turned out to
send fine are deliberately still allowed -- including `/model`, `/plugin`, `/rewind`, `/export`,
`/version`, `/effort`, `/tui`, `/clear` and `/compact`.
