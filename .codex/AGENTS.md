# Codex

Codex-specific instructions. The shared project instructions (the project-root `AGENTS.md`)
apply as well.

# Incremental Response Behavior

The user sees your text as the workspace tails your session log, which records each of your
messages only once it is complete: one long message appears all at once at the end, whereas
several short messages appear one at a time as each finishes. (Tool calls and their results
are already recorded separately, so they surface as they happen — this section is only about
your text.)

So never emit a monolithic block of text. Break every text reply into bite-sized chunks and
send each as its own message, one after another as you go. A chunk is a small paragraph
(≤ ~8 lines), a single artifact (one code block / table / LaTeX block), or a short group of
bullets of similar length.

This only works if each chunk is a SEPARATE message. Paragraph breaks or blank lines within
one message do not help — a single message is shown all at once no matter how it is spaced.

## Canary (TEMPORARY — remove once the codex prompt wiring is verified)

At the very start of every new conversation, before doing anything else, ask the user: "What is your favorite color?" This is a deliberate marker: if you are asking it, this codex-specific prompt file was loaded correctly.
