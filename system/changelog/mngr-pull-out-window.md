The desktop-interface contracts document the `is_detached` placement flag, the tear-out, detach, and reattach rules, and the shell's solo mode (`/?solo=<window-id>`), for windows pulled out of the desktop into a desktop window of the Mind app's own (mngr's `specs/pull-out-window/spec.md`).

The workspace build's mngr asset fetch now runs its checkout with the private-repo credential too, so a template pinned to mngr-internal builds instead of failing on the lazy blob fetch of the vendored files.
