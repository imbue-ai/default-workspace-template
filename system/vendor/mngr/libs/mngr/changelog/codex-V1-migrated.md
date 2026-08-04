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

A create template may now set a field on the agent type the create resolves to, not just a
`mngr create` option. A role writes `output_style = "..."` or
`append_system_prompt__extend = [...]` once and it lands on whichever type the template
stack selected, so no role names a harness. Keys are routed after every template applies
(a harness template is what sets the type) and compiled into settings entries, reusing the
existing template-contributed-settings fold. A key that is neither an option nor a field on
that type now RAISES, naming the template, the type and which types do support it --
previously it was silently dropped, so a typo or a role stacked onto a harness that could
not honour it produced an agent that quietly ignored part of its configuration.
