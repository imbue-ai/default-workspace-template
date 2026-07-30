Claude agents now honor mngr's harness-neutral `--output-style` and `--append-system-prompt`
create options.

`--output-style` becomes the `outputStyle` setting in the managed settings claude launches with,
applied after `settings_overrides` so a per-create style wins over a configured one without
disturbing the other resolved keys (model, fastMode, and the rest). The name is validated during
provisioning against `.claude/output-styles/` in the work dir -- the same directory claude itself
reads -- so a misspelled name or a broken symlink fails the create instead of producing an agent
that launches silently unstyled.

`--append-system-prompt` becomes claude's `--append-system-prompt` launch flag, emitted through
the new `build_extra_agent_args` hook. Create templates that previously spelled this out as
`agent_args = ["--append-system-prompt", "..."]` can use the harness-neutral option instead.
