Skills that launch a hardening worker now use `--template worker` instead of the removed
`subskill-worker` template: crystallize-creation, heal-creation, update-creation, and
update-system-interface. The generic `harden-worker` sub-skill they rely on is now installed
for every worker, so the separate template had nothing left to add.

Output styles move from `.claude/output-styles/` to `.agents/output-styles/`, with
`.claude/output-styles` a symlink to it -- the same arrangement `.claude/skills` already
uses. This makes them harness-neutral: claude reads them through its own path, while codex,
which has no output-style concept, reads the file body directly as developer instructions.

Codex agents are now told not to use their built-in `create_goal` / `get_goal` / `update_goal`
tools, alongside the existing `update_plan` ban. All four write to stores the user cannot see,
competing with the `tk` records that drive the chat progress view.
