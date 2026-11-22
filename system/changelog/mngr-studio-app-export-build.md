Apps can leave the workspace as Mac apps of their own (Imbue Studio's app export).

- In the Imbue Studio desktop app on macOS, dragging an app's desktop shortcut hands it to macOS as a Mac app of its own from the first move, with one icon the whole way: dropped on the Dock it is pinned there, dropped on the Desktop or in a Finder window it is copied there. Over the desktop the drag still rearranges the shortcuts and lands where it is released; Escape puts the shortcut back. Only apps a user or agent built can be exported, not the workspace's own system apps.

- The shortcut's and the launcher's menus offer "Add to Applications" and "Show in Finder" for those apps.

- The shell announces each app's `start_path` (the page its first desktop shortcut opens by GET), which an exported app opens on, and takes the embed contract's app-export messages (version 18). A Studio without app export sends none of them and the desktop behaves as before.
