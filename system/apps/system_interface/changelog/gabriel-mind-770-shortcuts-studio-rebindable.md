In the desktop app the keyboard shortcuts screen ends with an Imbue Studio section: New window, Close window, Quit and the zoom keys, which Imbue Studio hands the framed `/keyboard-shortcuts` page with `minds:chrome-shortcuts`. They live in the account, so a row offers one Save, which asks Imbue Studio to store it (`minds:save-chrome-shortcut`) and reads "Saving..." until Imbue Studio sends the shortcuts again, then "Saved", or why the save failed; Reset goes back to the defaults the same way. The section is not shown in a browser.

Imbue Studio runs these shortcuts before any page sees the key, so they clash with every action of every app and every action clashes with them, and the recorder refuses one typing could trigger, as for a workspace-wide action. No browser runs them, so their rows never warn that a browser keeps a shortcut. Replace saves each action where it lives: a workspace action that an Imbue Studio shortcut takes a shortcut from is saved everywhere, and an Imbue Studio action that a workspace action takes one from is saved through Imbue Studio, whichever save is pressed; resets and discards follow the same rule.

The screen lists the apps in the launcher's order (each manifest's `launcher_rank`, then registry order), so Chat comes before Files wherever the apps happened to register.

An action takes at most 16 shortcuts: once it holds 16, Add shortcut is greyed out and says so on hover, and a save of more is refused.

A search field above the list keeps the actions whose names hold every typed word, in any order, with the words marked; each app shows how many of its shortcuts match, and an app with a match opens even if it was collapsed.
