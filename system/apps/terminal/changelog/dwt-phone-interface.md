The terminal on a phone (`docs/system/blueprint/desktop-interface/plan-phone-interface.md`):

- Under 700px of width the terminal page shows a key strip under the terminal, above the soft keyboard: Esc, Tab, Ctrl and the four arrows. Ctrl is one-shot: it applies to the next key, whether from the strip or typed on the keyboard, and then releases. The strip's keys never take focus, so the keyboard stays up while you use them.

- Tapping the terminal focuses it from inside the terminal's own page, which is what lets iOS raise the soft keyboard (a focus passed in from the surrounding page cannot); a swipe to scroll does not.

- The page fills the dynamic viewport (`100dvh`), and when the visual viewport resizes (a soft keyboard opening or closing) the terminal refits its grid once it settles.

- The one-pixel nudge that makes ttyd refit after its page loads now actually resizes the terminal: the size was restored before the page was ever laid out at the smaller size, so the nudge had been a no-op.

- The installed ttyd web client carries a small added script that feeds the strip's keys to xterm with the sequences xterm itself sends (application cursor mode respected for the arrows); a client with no closing body tag is installed as it is, without the phone keys, with a warning.
