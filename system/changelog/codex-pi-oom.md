Every harness now launches through the OOM band wrapper, not just claude.

`[agent_types.codex]` and `[agent_types.pi-coding]` in `.mngr/settings.toml` gained the
`command` they were missing. Without it those agents ran unbanded, so under sustained
memory pressure earlyoom shed them by kernel score rather than by the user/worker tiering
-- meaning it could take a user's agent before a worker's build subprocess.

The wrapper is now `agent_oom_launch.py` and names the harness binary in the `command`
itself (`... agent_oom_launch.py codex`), so mngr splices its own flags after that base
exactly as it would after a bare binary name.
