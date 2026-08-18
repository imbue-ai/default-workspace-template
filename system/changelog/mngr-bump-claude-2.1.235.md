Bump the pinned Claude Code CLI from 2.1.207 to 2.1.235 across the workspace image: the
agent-config pin in `.mngr/settings.toml` (`[agent_types.claude].version`), the workspace
`system/Dockerfile` build arg, and the `CLAUDE_CODE_VERSION` default in
`system/scripts/setup_system.sh`.

Lands together with the matching bump in mngr-internal (the release-sandbox Dockerfile and CI
pins). `test_claude_version_alignment` asserts the two repos agree; provisioning fails with
"Claude version mismatch" if they drift.
