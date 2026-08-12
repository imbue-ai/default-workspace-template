Message-lifecycle-contract prep, shared across the chat harnesses:

- The stop button now clears any lingering "Sending…" indicators on a successful interrupt, so a
  message handed back to the composer no longer leaves a ghost bubble behind. It clears only the
  indicators that existed before the interrupt, so a message you send while the interrupt is in
  flight is untouched.

- The shoulder tap no longer pops an error when it races a message send it can't act on; it is now
  a quiet no-op (the button is already greyed while a send is in flight), removing the
  button-then-error case.

- Internal: the per-message "Sending" tracking that Claude used is now shared code that the Pi
  harness inherits too, so both track in-flight sends the same way.
