The keyboard shortcuts screen reads shortcuts in a context: the platform, and whether the workspace runs in the desktop app or in a browser. The chrome tells the framed `/keyboard-shortcuts` page which with `minds:embedder-capabilities` (one that can pull a window out is the desktop app); the in-workspace dialog reads the same flag off the shell.

- `meta` is the primary modifier: ⌘ on a Mac, Ctrl on Windows and Linux, so every shortcut is written once and shows as the key the platform has. A Mac's Control shortcut is hidden off a Mac, and a shortcut a browser keeps for itself (⌘W, ⌘T, ⌘N and the like) is hidden when the workspace runs in a browser. A row whose shortcuts are all hidden here reads "Off" and names them on hover; hidden shortcuts take part in no clash.

- The reserved-shortcut warning is replaced by three that never block a save: a shortcut a browser would keep (shown in the desktop app), one the OS is known to keep for itself (⌘Tab, Alt+Tab, and the rest), and one the desktop app takes from the OS (Alt+F4 on Windows). Only Ctrl+Alt+Delete is refused by the recorder, with a reason.

- The launcher's Ctrl+Enter reads the same way: on Windows and Linux the Control key is `meta`, and the Windows key is no modifier.
