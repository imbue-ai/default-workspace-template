The Claude shoulder tap no longer silently loses a message that its cancel caught mid-turn.

Before: Claude can fold a queued message into the turn that is already running, at its next tool result. If the tap's cancel then lands on that turn, the message is never answered. The tap is built to catch this and send a recovery nudge, but it decided whether a new turn had started from mngr's `active` marker. An interrupt fires no Stop hook, so the cancelled turn's marker was still on disk, and the tap reported success with nothing running.

Now the tap decides from the session ledger. A `dequeue` after the chord means the queued prompt started its own turn. An assistant record counts as that turn's answer only once it follows the interrupt sentinel, because before the sentinel the turn being cancelled is still writing. Without either, the tap sends the recovery nudge, which starts the turn that answers the message.

A new `real_claude` test drives both cases against the pinned Claude Code: the cancelled follow-on gets the nudge and an answer, and a flushed turn that answers slowly gets none.
