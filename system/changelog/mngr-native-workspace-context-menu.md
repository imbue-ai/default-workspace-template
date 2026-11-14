Every right-click menu in the workspace is now built from the same rows, whether the app is built in or one an agent built: an app's own rows can be actions with an icon and a tone, check rows, and submenus, and the menu follows the workspace theme in both. `.agents/shared/references/right-click-menus.md` says how an app gives an element its own rows.

A new check, `system/test_context_menu_rules.py`, fails on any app's right-click handler that prevents the default (it calls `preventDefault`, or it is an `oncontextmenu` handler that returns `false`), the user's apps included, unless an `// own-context-menu: <reason> -- <why>` marker above it says why it keeps the right-click. It reads frontend sources and the pages an app keeps as strings in its Python files, as a scaffolded app's `runner.py` does.

In the Imbue Studio desktop app, right-clicking text you are editing, text you have selected, or a plain link inside the workspace now opens the system's native menu (spelling suggestions, Look Up, Cut / Copy / Paste, Copy Link, Share), with none of the workspace's rows in it. Everything else keeps the workspace's own menu, and so does every right-click in a plain browser or an older desktop app. Holding Shift while right-clicking in the desktop app gives the native menu wherever the element menu takes the right-click; a view that handles a right-click itself keeps its own.

Right-clicking an image now offers "Copy image" where the page can read the image.

The element-reference-menu plan (sections 3.3, 4.1, 4.4, 6, 7.3, 9, 11, and 13) and contracts.md's `shell:handshake` row describe the new behavior.
