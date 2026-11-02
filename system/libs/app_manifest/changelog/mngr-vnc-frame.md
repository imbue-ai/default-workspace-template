`find_wiring_sections` accepts a declared wiring program whose supervisord block is an `[eventlistener:...]` section (the Screen's `screen-watch`), attributing it to its drop-in like a program's block.

The README's footprint description uses the Screen (its stack's programs and `screen-watch`, each in its own drop-in) as the `wiring` example in place of the deleted browser app and its `xvfb`.
