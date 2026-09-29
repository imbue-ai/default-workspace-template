Prose across the workspace now calls the desktop app Imbue Studio and the thing you talk to "your agent". The desktop app is now called Imbue Studio, and the noun for the thing you talk to is now "your agent" rather than "your mind".

The root README, AGENTS.md, the `data/` READMEs, the workspace-internals doc and the `.mngr` create templates are reworded, including the five systemd unit `Description=` strings that a user sees in `systemctl status`.

The unit names themselves (`minds-autostart.service`, `minds-autostart.path`), the `minds-caretaker` cron drop-in, the `bootstrap@minds.local` git identity, the `minds patch` marker blocks that `install_dufs.sh` re-applies by name, and the `minds-v*` tag scheme are all unchanged: they are identifiers and live state, not display text.

`docs/system/blueprint/` and `catalog/` are deliberately untouched -- the first is dated design history, the second is generated and carries third-party authors' own project names.
