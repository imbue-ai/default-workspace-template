# Minds persona eval: $case_id

This file describes the trial to the people who run the eval and read its
results; no model reads it. The client side of the conversation is played by
the minds_evals driver, a deterministic harness that creates a workspace through
the box's Minds API, sends each entry below when the workspace agent is WAITING,
and records the conversation as an ATIF trajectory at
`/logs/agent/trajectory.json` (with progress state in
`/logs/agent/state.json`). A model is consulted only inside an entry that calls
for one, and never holds the conversation loop itself.

Persona: $persona_prose

Entries. A literal message is sent verbatim; `DECIDE_FROM_PERSONA` is a message
the persona model writes in character; a goal entry is not a message at all, but
a stretch of conversation the client model keeps going, within its exchange
budget, until it says the goal is met:

$prompts_prose

The machine-readable case config. This fenced block is the only part of the
file the driver reads:

```json
$case_config_json
```
