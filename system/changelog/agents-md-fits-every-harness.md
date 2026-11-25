Every agent harness now reads the whole of `AGENTS.md`. Antigravity truncates any rule file past 24,000 bytes, with no setting to raise it, and Codex read only 32,768 bytes, so on a 47,845-byte file both lost the end of their standing instructions.

- `AGENTS.md` is rewritten from 47,845 to 22,994 bytes. The same instructions are stated in fewer words; text a skill or shared reference already says is cut; and detail needed only at one step (designing a creation, answering a chat room, driving Fortress from Playwright, escalating a built-in defect, a command that fails) moves to a shared reference that `AGENTS.md` names at that step.

- Codex agents read up to 65,536 bytes of `AGENTS.md` (`project_doc_max_bytes` in `[agent_types.codex]`).

- `system/test_agents_md_size.py` (new) fails when `AGENTS.md` exceeds the smallest limit across the harnesses: Antigravity's fixed 24,000 bytes, or Codex's configured budget if lower.

- `docs/system/app-icons.md` lists `update-app`, not `AGENTS.md`, among the places that restate the icon rules.
