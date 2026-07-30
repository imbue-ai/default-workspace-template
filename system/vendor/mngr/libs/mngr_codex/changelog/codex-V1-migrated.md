Fixed intermittent "Sending…/Queued…" hangs (and eventual send failures) when
messaging a codex agent after a turn that produced substantial output. The
pre-send readiness poll looked for codex's `/model to change` header, which sits
at the top of the TUI and scrolls out of the visible pane once a turn renders
enough output -- so the poll could not confirm the composer was ready and
withheld the paste until it timed out. Readiness now keys off the composer
prompt glyph (`›`), which is pinned at the bottom input line and never scrolls
off -- the same approach `mngr_claude` already uses with `❯`.

Codex agents now honor mngr's `--output-style` and `--append-system-prompt`. Codex has no
output-style concept, so both are joined into the top-level `developer_instructions` key of the
per-agent `config.toml` -- the key that appends to codex's built-in instructions (unlike
`model_instructions_file`, which replaces them). The style file's body is used verbatim,
frontmatter block included, so a style reads the same whichever harness runs it. Style names are
resolved from `.agents/output-styles/` in the work dir.

Known limit: a style that suppresses a harness's built-in prompt cannot behave identically here,
because `developer_instructions` can only append.
