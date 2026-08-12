The Pi chat integration now follows the message-lifecycle contract the way Claude does:

- Interrupting Pi now brings a message that was still sending back into your composer instead of
  dropping it, alongside any queued messages, in the order you sent them.

- Queued-message chips appear and clear promptly (the two-second delay is gone), and a chip is
  removed before its message shows up as a turn, so a message is no longer shown twice at once.

- The shoulder-tap button is now controlled entirely by the backend: it greys itself whenever a
  send is in flight or there is nothing to tap, and pressing it can never produce an error -- it
  just quietly does nothing if it can't act. The frontend no longer decides any of this and no
  longer special-cases the integration.

The rare remaining edge cases (brief visual flickers, and one uncommon message-loss on interrupt
during a shoulder-tap flush) are documented in the Pi harness folder
(`harnesses/pi_coding/message_lifecycle_limitations.md`).
