# Minds persona eval: $case_id -- step $step_number of $step_total ($step_name)

This file describes one step of the trial to the people who run the eval and
read its results; no model reads it. The client side of the conversation is
played by the minds_evals driver, a deterministic harness that harbor calls once
per step. The workspace, and the conversation inside it, are the same ones the
earlier steps left behind: the first step creates the workspace, and it is torn
down by the last step or by whichever step the trial gave up on. Every step
collects verification evidence before it ends, against its own expectations, and
records the conversation so far as an ATIF trajectory at
`/logs/agent/trajectory.json` (with progress state in `/logs/agent/state.json`).
A model is consulted only inside an entry that calls for one, and never holds the
conversation loop itself.

Persona: $persona_prose

$files_prose

This step's entries. A literal message is sent verbatim; `DECIDE_FROM_PERSONA`
is a message the persona model writes in character; a goal entry is not a
message at all, but a stretch of conversation the client model keeps going,
within its exchange budget, until it says the goal is met:

$prompts_prose

$expectations_prose

The machine-readable case config for this step. This fenced block is the only
part of the file the driver reads:

```json
$case_config_json
```
