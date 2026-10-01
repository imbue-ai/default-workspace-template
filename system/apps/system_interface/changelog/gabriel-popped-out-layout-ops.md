An agent's layout ops no longer quietly undo a window the user popped out into its own Imbue Studio window.

- `restore`, `maximize`, `place`, and `minimize` on a window the target client has popped out (while it is connected) are refused with 423 and change nothing, including the switch `args.desktop` would make. With `force` they apply and bring the window back onto its own desktop without switching the client's desktop window; a forced `minimize` brings it back minimized, so it actually goes out of sight instead of only its ghost. A disconnected client's pulled-out window is moved without `force`, so the pop-out reopened at its next launch closes.

- `focus`, and an `open` that finds the window at the path, raise the pop-out's own window instead of pulling it back. `open --beside` a popped-out window opens unpaired unless forced. `close`, `navigate`, `refresh`, `show`, and the rest are never refused.

- The op answers say what happened: raised in its own window, brought back, not paired, and whether the client has only pop-outs open (so a window shown on its desktop waits for a desktop window). The inventory lists each client's popped-out windows with whether their ghost is hidden.

- A pulled-out window's own page raises its window when a `show`, a `focus`, or an `open` that finds it names it, so the raise works with the main window closed.
