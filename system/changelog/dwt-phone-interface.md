The phone interface plan is in `docs/system/blueprint/desktop-interface/plan-phone-interface.md`, with the desktop plan, the concepts, and the contracts brought in line, and the launcher and pinned-entries plans pointing at it: compact mode is gone, and the contracts carry the phone's routes, client history, `layout_op` announcements, tokens, and test selectors.

- `layout.py desktops` and `list` show each client's `shown_history`: what its phone layout showed, most recent last.
