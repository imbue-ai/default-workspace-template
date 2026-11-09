`keybindings.ts` gains `chordKeycapLabels`, which turns a canonical chord into the keycaps to draw (Apple's modifier symbols on an Apple platform, spelled-out names elsewhere), `chordSpokenLabel`, which names it in words ("Command+Shift+N") for tooltips and screen readers, and `isApplePlatform`, the platform check the shell's launcher already used.

`embed.ts` exports `KEYBOARD_SHORTCUTS_READY` (`minds:keyboard-shortcuts-ready`, embed contract v15), probed like the other newer types, which the shell's keyboard shortcuts page sends once it has loaded.
