The notify-user skill says a reply posted into a chat room (`mngr message --start <room>`) is the notification, so the script is not run for one.

The `manage-desktop` skill describes desktops as belonging to people by convention (a desktop's `owner`, the "Left behind" desktops of people who have gone) and the shell's overview, which replaces the taskbar's desktop switcher as the place a person creates, renames, reorders, and deletes their desktops; a desktop's colour and glyph are no longer shown, so an agent names a desktop and leaves the rest to the shell (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 8).

The `build-app` skill gains "Renaming an app": when an app's `display_name` changes, rename the chat titled "<old> Chat" to "<new> Chat" with `system/scripts/rename_chat.py`; the button finds the chat by its `app_chat` label whatever its title, so the title is what the rename keeps true (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 9.3).

The `update-self` skill states the frontend-rebuild contract plainly: an update touching any frontend source or the shared `workspace_ui` library (which carries behavior the shell depends on from every app page, like the `shell:input` activity reports) lands only with the bundles rebuilt; its apply does this itself, and the rule guards hand-applied updates.
