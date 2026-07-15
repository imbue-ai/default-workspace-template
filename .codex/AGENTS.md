# Codex

These notes are specific to running as Codex. The shared project instructions (the project-root `AGENTS.md`) apply as well.

## Memory

You have no automatic memory loading. At the start of a session, read any markdown files under `runtime/memory/` yourself, and persist durable facts there as new markdown files. (The `runtime/memory/` location and its runtime-backup behavior are described in the project instructions.)

## Canary (TEMPORARY — remove once the codex prompt wiring is verified)

At the very start of every new conversation, before doing anything else, ask the user: "What is your favorite color?" This is a deliberate marker: if you are asking it, this codex-specific prompt file was loaded correctly.
