The Caretaker's tab now blinks again on every run, and the blink is now a reusable building block:

- Previously, if you'd left the Caretaker's tab open, it would not blink again on later runs -- only a closed tab re-surfaced. Now the tab re-blinks for each new run whether it was closed (re-opened) or just sitting open in the background, so you always notice when the Caretaker has run again. (A tab you're actively looking at is left alone -- no point blinking what's already in front of you.)

- The blink is now driven by a generic `highlight` label rather than the Caretaker-specific `auto_created` label. Any agent can opt its tab into the blink by carrying a `highlight` label; the label's value is a key that, when bumped, makes the tab blink again. This generalizes the behavior beyond the Caretaker.

- The Caretaker's tab now opens in the main chat window, tabbed alongside your initial chat, instead of landing in whatever pane happened to be active (or a separate split).

- The blink now fires reliably even when a run happens while the workspace is down and is picked up at startup (catch-up), and even if you'd already opened the tab in a past session. The "have you seen this tab" state now remembers *which run* you acknowledged (by viewing the tab) rather than a one-time seen flag, so a brand-new run always re-blinks the tab -- including one that was restored already-open when the workspace came back up.
