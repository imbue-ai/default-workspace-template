Apps can leave the workspace as Mac apps of their own (Imbue Studio's app export).

- In the Imbue Studio desktop app on macOS, dragging an app's desktop shortcut out of the Studio window hands it to macOS as a Mac app of its own: dropped on the Dock it is pinned there, dropped on the Desktop or in a Finder window it is copied there. Inside the window the drag still rearranges the desktop. Only apps a user or agent built can be exported, not the workspace's own system apps.

- The shortcut's and the launcher's menus offer "Add to Applications" and "Show in Finder" for those apps.

- The shell announces each app's `start_path` (the page its desktop shortcut opens by GET), which an exported app opens on, and takes the embed contract's app-export messages (version 12). A Studio without app export sends none of them and the desktop behaves as before.
