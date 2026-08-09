The claude agent type gains two settings, `output_style` and `append_system_prompt`, that a
create template can set so a role describes claude's behaviour without spelling out argv.

`output_style` becomes the `outputStyle` setting in the managed settings claude launches with,
applied after `settings_overrides` so a role's style wins over a configured one without
disturbing the other resolved keys (model, fastMode, and the rest). The name is validated during
provisioning against `.claude/output-styles/` in the work dir -- the same directory claude itself
reads -- so a misspelled name or a broken symlink fails the create instead of producing an agent
that launches silently unstyled.

`append_system_prompt` is a list, so `append_system_prompt__extend = [...]` in a template makes
each stacked role contribute a block. The blocks are joined into ONE `--append-system-prompt`
launch flag: claude's flag is last-wins (verified against 2.1.220), so passing it per block
would deliver only the final one and silently drop every role stacked before it. Create
templates that previously spelled this out as `agent_args = ["--append-system-prompt", "..."]`
can use the setting instead.

The `model_state_hook.py` Claude Code hook (and its SessionStart/UserPromptSubmit/PostToolUse/Stop
registrations) is removed. The chat model bar's live model/effort/fast now comes from the
workspace's statusline command, which Claude Code re-runs on every session start, assistant
message, and refresh tick -- strictly more reactive than the hook (which could not see idle
`/model` switches) and immune to the `<synthetic>` model ids the hook could record from
framework-generated transcript messages. The statusline writes the harness-uniform
`$MNGR_AGENT_STATE_DIR/minds_model_state.json` snapshot that the system interface reads for
every harness.
