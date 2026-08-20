# Escape, then send

Walkthrough of the planned send path, and how blocking surfaces error out.

---

Yes — this is the send path, and it's mostly *replacing*, not adding. One new helper; the rest are five existing checks that each swap "look at the whole pane" for "look at the bottom of the pane."

**New:** a helper that returns the bottom of the pane — the last 8 non-blank lines. Nothing else about it.

**Replaced:** the early bail-out at the top of `classify`; the whole-pane search inside `has_input_prompt_line`; the footer test in `GenericBenign`; the unbounded upward scan in `_detect_preexisting_input_text`; and the readiness indicator, which today is the raw `^❯` pattern.

**Unchanged:** the order of steps in a send, and the dismissal loop.

---

Now a send, step by step.

The lock is taken so two sends can't interleave. Then preflight starts, and preflight is the dismissal loop.

The loop grabs a snapshot of the pane and hands it to `classify`. Today `classify`'s first act is to look anywhere in that snapshot for a `❯` at the start of a line, and if it finds one, declare the input free. That line goes away. Instead it walks the dialog catalogue in order and asks each entry whether it matches this pane — shell mode entries are in that same list, so they're checked here too, not separately.

If one matches, `classify` returns it, and that's the answer. No input-box question was asked at all.

If none matches, only then does `classify` ask whether the input box is on screen — and it asks that of the bottom 8 non-blank lines rather than the whole snapshot. If the box is there, it returns "nothing is holding the input." If it isn't there, something owns the pane that we can't name, and it returns `Unrecognized`.

Back in the loop: if the answer was "nothing," preflight is done. If it was a dialog, the loop tells that dialog to deal with itself — Escape for a dismissible one, Backspace for empty shell mode, cycle-then-Enter for one the operator opted into answering, or an immediate refusal for one only a human should touch. Then it re-captures and asks again, because dismissing one surface can reveal another beneath it. If the same thing is still there after acting on it, it's stuck rather than chained, and the loop refuses instead of pressing the same key at it forever.

Once preflight reports clear, the readiness wait runs — and now it means something. Today it's handed the bare `^❯` pattern and matches it against the whole pane, which any conversation with history satisfies instantly, so it never waits for anything. It gets the region-aware question instead, so it genuinely waits for the input box to appear at the bottom.

The shell-mode checks read the same bottom region as everything else. That matters more than it sounds: the shell-mode footer text and a bare `!` row both occur in ordinary conversation — an agent merely discussing shell mode has them on screen — and shell mode is classified before the dialog surfaces, so a whole-pane test would claim shell mode for a pane holding a settings window and refuse the send naming a command that doesn't exist.

Then the leftover-text check. It scans upward from the bottom for the last line beginning with `❯` and treats what follows as text already sitting in the composer. That direction is right; what's missing is a floor, so today it keeps climbing into the transcript and finds your previous message. Bounded to the region, it finds nothing when the box isn't drawn — so instead of announcing it will append to a past turn and then timing out ninety seconds later, the send stops on the real problem.

Then the paste, the paste-visibility check, and the submit-and-confirm, all unchanged.

One more replacement worth naming: the benign catch-all, the entry that recognizes a dialog purely by an "Esc to close / Esc to cancel" footer. It's currently protected by that deleted bail-out — without it, a message whose *text* quotes those words would look like a dialog. So it now requires the footer to appear within the last 3 non-blank lines. A real dialog's footer is down there; a quote in a transcript isn't.

---

Same mechanism, different message — `PendingShellCommand` **is** a `Blocking` subclass, so it errs out through exactly the same path.

The loop doesn't distinguish them. Any blocking surface raises `DialogBlocked` from inside `deal_with_dialogs`, which unwinds the whole loop immediately — no keys pressed, nothing dismissed. The plugin catches it at the boundary and picks the final error type:

- `PendingShellCommand` → `ShellCommandPendingError`, whose wording tells you to open the terminal and press Enter to run your half-typed command or Escape to cancel it.
- Everything else, including `Unrecognized` → `DialogDetectedError` carrying that dialog's own message.

That special-case exists only so the shell-mode error can say something actionable about *your* command; a generic "a dialog is open" wouldn't. Structurally it's one path.

Three other things raise the same way: a surface still present after being acted on (stuck, not chained), the pass limit, and the permission-prompt check that runs before the loop.

Empty shell mode is the odd one out — it's `SelfClearing`, not `Blocking`, because only mngr's own send can produce it, so mngr backspaces out of its own mess rather than erroring at you.

**None of that changes in the plan.** But here's the consequence worth flagging: both shell-mode predicates currently ask "is the input box absent?" of the whole pane, so a transcript echo makes them answer "no" and they never fire. They're dead today. After the fix they start working — which means you'll begin seeing `ShellCommandPendingError` in cases that currently fail some other way, silently or with a 90-second timeout. That's correct behavior appearing for the first time, not a regression, but it will look like a new error.
