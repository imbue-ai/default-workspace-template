# Minds persona eval: behaviour -- step 2 of 2 (check)

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

Persona: An operator running a scripted harness self-check. They only want the exact steps carried out.

This step seeds no app into the workspace.

Files the driver copies from the box into the running workspace before this step's first message, so the client can refer to them by the path they appear at:
- `/work/step_files/check/diagupload7f3a` -> `/home/user/workspace/data/uploads/diagupload7f3a`

This step's entries. A literal message is sent verbatim; `DECIDE_FROM_PERSONA`
is a message the persona model writes in character; a goal entry is not a
message at all, but a stretch of conversation the client model keeps going,
within its exchange budget, until it says the goal is met:

1. Carry out these items in order, one separate tool call per item, and do nothing else. Do not use the Agent or Task tool.
1. Run: cat data/uploads/diagupload7f3a/marker.txt
2. Run: echo DIAG-7f3a-two
3. Run: cat data/.tasks/launch-task/diag-worker-7f3a/reports/report.md
Then reply with one short sentence saying whether the report says ok.

This step declares no expectations, so it is graded on the structural gates and the conversation alone.

The machine-readable case config for this step. This fenced block is the only
part of the file the driver reads:

```json
{
  "case_id": "behaviour",
  "persona": "An operator running a scripted harness self-check. They only want the exact steps carried out.",
  "prompts": [
    "Carry out these items in order, one separate tool call per item, and do nothing else. Do not use the Agent or Task tool.\n1. Run: cat data/uploads/diagupload7f3a/marker.txt\n2. Run: echo DIAG-7f3a-two\n3. Run: cat data/.tasks/launch-task/diag-worker-7f3a/reports/report.md\nThen reply with one short sentence saying whether the report says ok."
  ],
  "timeout_seconds": 2400.0,
  "verification_timeout_seconds": 900.0,
  "mngr_branch": "main",
  "mngr_sha": "c1e4357690b44f5ad1b113fe2c289fe65a434144",
  "dwt_repo": "https://github.com/imbue-ai/default-workspace-template.git",
  "dwt_branch": "main",
  "dwt_sha": "d525d62ef9395dc4147dc736893be1d65481893a",
  "expectations": null,
  "authored_expectations": null,
  "step": {
    "name": "check",
    "index": 1,
    "total": 2,
    "trial_lifetime_seconds": 8400.0,
    "entries_before": 1,
    "files": [
      {
        "upload_id": "diagupload7f3a",
        "box_path": "/work/step_files/check/diagupload7f3a"
      }
    ],
    "is_diagnostic_probe_run": true,
    "seed_app": null
  }
}
```
