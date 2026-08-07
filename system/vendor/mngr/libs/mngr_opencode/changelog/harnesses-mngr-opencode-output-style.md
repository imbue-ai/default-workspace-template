The opencode agent type now supports the `output_style` and `append_system_prompt`
settings (like claude/codex/pi/antigravity). Their concatenated text -- the appended
system-prompt blocks first, then the output-style body -- is written to the per-agent
`AGENTS.md` under the opencode config dir, which opencode auto-loads as its global rules
(additive with the project's own `AGENTS.md`). Nothing is written when a role contributes
neither.

This also lets the shared `chat` create template (which sets `output_style`) resolve
against opencode instead of being rejected, so `mngr create --type opencode --template
chat` now works.
