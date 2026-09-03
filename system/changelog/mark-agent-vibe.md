Restored the AGENTS.md / CLAUDE.md split. `AGENTS.md` is the single shared
body every harness reads; `CLAUDE.md` is now just `@AGENTS.md` plus the
Claude-specific delta (the Memory section, which describes Claude's built-in
memory system and `autoMemoryDirectory` and means nothing to codex, pi, agy or
opencode). This is the design from `dev/changelog/claude-codex-pi-dwt.md`, lost
as collateral damage in b17ccea22's wholesale revert of main; since then both
files carried the same ~280 lines and had drifted against each other.

Wording that existed only in CLAUDE.md was ported into AGENTS.md before the
truncation, so nothing was dropped: the bare-slash-command rule, the browser
tooling section, the `mngr` ModuleNotFoundError guidance (which of the two
installs to refresh), the mngr-upstream-PR pointer, the automations machinery
pointer, and the inspiration-to-template rename.

The `tk` task-management section is sharper about step records being the
replacement for the disabled `TodoWrite`, and about step titles and summaries
being user-facing copy -- with a worked example contrasting casual phrasing
against technical precision.
