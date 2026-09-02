# Minds persona eval: $case_id

Drive a multi-turn conversation with a real Minds workspace agent, playing a
client whose persona and scripted turns are given below. Create the workspace
through the box's Minds API, send each turn when the agent is WAITING, and
record the conversation as an ATIF trajectory at `/logs/agent/trajectory.json`
(with progress state in `/logs/agent/state.json`).

Persona: $persona_prose

Turns. A literal message is sent verbatim; `DECIDE_FROM_PERSONA` is role-played
from the persona; a goal entry is not a message at all, but a stretch of
conversation the client keeps going, within its exchange budget, until it says
the goal is met:

$prompts_prose

The machine-readable case config (parsed by the driver agent):

```json
$case_config_json
```
