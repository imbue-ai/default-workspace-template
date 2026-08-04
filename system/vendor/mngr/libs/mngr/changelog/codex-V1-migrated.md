A create template may now set a field on the agent type the create resolves to, not just a
`mngr create` option. This lets a template describe an agent's *role* without naming the
harness that will run it: a role writes `output_style = "..."` or
`append_system_prompt__extend = [...]` once and it lands on whichever type the template stack
selected. Previously the only way to express either was `agent_args`, which is raw argv and
therefore harness-specific, so a role that needed one had to be duplicated per harness.

An output style is a markdown file whose frontmatter sets `name:`; `output_style` takes that
display name, not a filename. The name is resolved and validated during provisioning, and an
unknown name fails the create with the available names listed, rather than silently launching
an agent with no style applied.

Routing happens after every template applies, because a harness template is what sets the
type. Keys compile into settings entries and reuse the existing template-contributed-settings
fold, so the operator suffixes behave exactly as they do everywhere else: a bare key assigns
(the last role in the stack wins) and `__extend` accumulates. Keys are combined per field
before being emitted, because each settings entry is applied against the base config
independently -- two `__extend` entries for one field would each extend the empty base and the
later would simply win, dropping the earlier role's contribution.

A key that is neither an option nor a field on that type now RAISES, naming the template, the
type, and which types do support it. Previously it was silently dropped, so a typo -- or a role
stacked onto a harness that could not honour it -- produced an agent that quietly ignored part
of its configuration. The two role fields live on the harness config subclasses rather than on
the base `AgentTypeConfig`, so a harness with no support for them has no field to route to and
the create fails naming the template instead of launching a misconfigured agent.
