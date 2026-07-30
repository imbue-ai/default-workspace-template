Made the TUI-ready timeout overridable per agent type (`get_tui_ready_timeout_seconds`),
so a harness whose startup legitimately renders the composer late -- e.g. a cold codex
resume that replays the whole rollout -- waits long enough instead of turning a
slow-but-fine resume into a hard send failure.

Added two harness-neutral create options, `--output-style` and `--append-system-prompt`, so
a create template can describe an agent's *role* without naming the harness that will run it.
Previously the only way to express either was `agent_args`, which is raw argv and therefore
harness-specific -- so a role that needed one had to be duplicated per harness.

An output style is a markdown file whose frontmatter sets `name:`; `--output-style` takes that
display name, not a filename. The name is resolved and validated during provisioning, and an
unknown name fails the create with the available names listed, rather than silently launching
an agent with no style applied.

Each agent type decides how to apply the two values. Types that turn them into launch flags do
so through a new optional `build_extra_agent_args` hook (default: no extra args), since
`assemble_command` sees only `agent_args`; types that consume them at provision time, by writing
a config file, override nothing.
