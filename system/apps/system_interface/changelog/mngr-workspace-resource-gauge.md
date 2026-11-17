The taskbar's tray has a resource gauge that says how much room the workspace has left.

- When the disk, memory or CPU runs short, the tray shows a warning in words ("Disk space is low"). Resting the pointer on it opens a card: what each resource has left ("3.2 GB left of 44 GB, full in about 12 minutes"), and the programs stopped to free memory in the last day. Clicking the warning or a row opens System Monitor on that resource.

- "Resource gauge..." on the desktop's right-click menu (also on the gauge and its card) is the owner's settings: show three small CPU, RAM and Disk bars always or nothing until a warning, and each warning's level. The disk warns when 5 GB or less is left (and whenever it is on course to fill within 15 minutes); memory and CPU warnings are off until turned on.

- A warning sends one Imbue Studio notification, which opens System Monitor on that resource. It is retried until delivered and is not repeated by a restart. A program the memory guard stops is reported the same way.

- While a warning stands, claude and codex agents are told at their next message, so they hold off on large writes or memory-heavy work and tell the user.

- The gauge is pushed to the windows over the shell's socket; a gauge that stops arriving is drawn as not updating rather than left showing its last figures.
