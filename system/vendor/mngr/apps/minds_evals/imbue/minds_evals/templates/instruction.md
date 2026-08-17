# Minds persona eval: $case_id

Drive a multi-turn conversation with a real Minds workspace agent, playing a
client whose persona and scripted turns are given below. Create the workspace
through the box's Minds API, send each turn when the agent is WAITING, and
record the full transcript to `/logs/agent/full_transcript.jsonl` (with
progress state in `/logs/agent/state.json`).

Persona: $persona_prose

Turns (literal message, or `DECIDE_FROM_PERSONA` role-played from the persona):

$prompts_prose

The machine-readable case config (parsed by the driver agent):

```json
$case_config_json
```
