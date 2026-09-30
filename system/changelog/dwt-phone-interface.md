The phone interface plan and its interactive mock are in `docs/system/blueprint/desktop-interface/` (`plan-phone-interface.md`, `phone-mock/`), with the desktop plan, the concepts, and the contracts brought in line: compact mode is gone, and the contracts carry the phone's routes, client history, `layout_op` announcements, tokens, and test selectors.

- `layout.py desktops` and `list` show each client's `shown_history`: what its phone layout showed, most recent last.
