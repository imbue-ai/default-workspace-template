Experiment: every window opened on the desktop now opens straight into its own Imbue Studio desktop window instead of on the in-workspace desktop. This covers windows you open yourself and windows an agent opens for you. Pinned windows stay on the desktop.

Closing one of those desktop windows now closes the window instead of putting it back on the in-workspace desktop. Dropping a desktop window onto the main window still docks it, and a window you docked stays docked when you open it again. The `IS_ALWAYS_POPPING_OUT` constant in the shell's `index.ts` turns the experiment off.
