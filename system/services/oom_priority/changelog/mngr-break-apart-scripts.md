The five plain-python3 OOM entry points (`oom_tag_service.py`, `oom_tag_backstop.py`, `earlyoom_record_shed.py`, `claude_oom_launch.py`, `claude_shed_notice_hook.py`) move out of `system/scripts/` into this package's `bin/`, next to the `src/` they import via `sys.path` insert (now a short sibling path instead of a cross-tree reach).

All wiring (supervisord command prefixes, the earlyoom `-N` hook, the claude launch `command` in `.mngr/settings.toml`, the SessionStart shed-notice hook) points at the new `system/services/oom_priority/bin/` paths, and the import-path guard test now scans both `bin/` and the remaining `system/scripts/` hooks.

The package's bare-print and broad-catch ratchet counts rise because the moved scripts (previously outside any per-project ratchet scope) now count against this project -- no new violations were added.
