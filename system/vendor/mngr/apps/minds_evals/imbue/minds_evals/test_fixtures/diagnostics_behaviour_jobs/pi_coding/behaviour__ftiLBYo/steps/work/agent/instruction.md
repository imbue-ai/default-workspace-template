# Minds persona eval: behaviour -- step 1 of 2 (work)

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

This step introduces no files into the workspace.

This step's entries. A literal message is sent verbatim; `DECIDE_FROM_PERSONA`
is a message the persona model writes in character; a goal entry is not a
message at all, but a stretch of conversation the client model keeps going,
within its exchange budget, until it says the goal is met:

1. This is an automated harness self-check. Carry out these items in order, exactly as written, one separate tool call per numbered item, and do nothing else. Do not use the Agent or Task tool, and create no ticket or step other than the ones below.
1. Run: tk create --step "DIAG alpha 7f3a"
2. Run: tk create --step "DIAG beta 7f3a"
3. Run: tk start <the id tk printed for DIAG alpha 7f3a>
4. Run: echo DIAG-7f3a-one
5. Run: diag-missing-command-7f3a
6. Run: tk close <the id of DIAG alpha 7f3a> "alpha done 7f3a"
7. Run: tk create "DIAG regular ticket 7f3a" -t chore
8. Run: tk start <the id of DIAG beta 7f3a>
9. Run exactly this, as one command: mkdir -p data/.tasks/launch-task/diag-worker-7f3a && printf '%s\n' '---' 'finish_report_path: data/.tasks/launch-task/diag-worker-7f3a/reports/report.md' '---' '' '# Task: diagnostic worker 7f3a' '' 'Write a finish report whose body is the single word ok, following .agents/shared/references/worker-reporting.md with name: done, then stop.' > data/.tasks/launch-task/diag-worker-7f3a/task.md
10. Run exactly this, as one command: uv run .agents/skills/launch-task/scripts/create_worker.py launch --name diag-worker-7f3a --template worker --runtime-dir data/.tasks/launch-task/diag-worker-7f3a/ --task-file data/.tasks/launch-task/diag-worker-7f3a/task.md
11. Run: tk close <the id of DIAG beta 7f3a> "beta done 7f3a"
12. If you have a Skill tool, invoke the skill named build-app with it and stop as soon as it has loaded; otherwise run: cat .agents/skills/build-app/SKILL.md
When all twelve items are done, reply with the single word: done.

This step commissions no deliverable, so nothing is probed or bundled; it is judged from the conversation, the always-on workspace capture and any UI flows it declares, against: The agent carried out the twelve items of the harness self-check, one tool call each.

The machine-readable case config for this step. This fenced block is the only
part of the file the driver reads:

```json
{
  "case_id": "behaviour",
  "persona": "An operator running a scripted harness self-check. They only want the exact steps carried out.",
  "prompts": [
    "This is an automated harness self-check. Carry out these items in order, exactly as written, one separate tool call per numbered item, and do nothing else. Do not use the Agent or Task tool, and create no ticket or step other than the ones below.\n1. Run: tk create --step \"DIAG alpha 7f3a\"\n2. Run: tk create --step \"DIAG beta 7f3a\"\n3. Run: tk start <the id tk printed for DIAG alpha 7f3a>\n4. Run: echo DIAG-7f3a-one\n5. Run: diag-missing-command-7f3a\n6. Run: tk close <the id of DIAG alpha 7f3a> \"alpha done 7f3a\"\n7. Run: tk create \"DIAG regular ticket 7f3a\" -t chore\n8. Run: tk start <the id of DIAG beta 7f3a>\n9. Run exactly this, as one command: mkdir -p data/.tasks/launch-task/diag-worker-7f3a && printf '%s\\n' '---' 'finish_report_path: data/.tasks/launch-task/diag-worker-7f3a/reports/report.md' '---' '' '# Task: diagnostic worker 7f3a' '' 'Write a finish report whose body is the single word ok, following .agents/shared/references/worker-reporting.md with name: done, then stop.' > data/.tasks/launch-task/diag-worker-7f3a/task.md\n10. Run exactly this, as one command: uv run .agents/skills/launch-task/scripts/create_worker.py launch --name diag-worker-7f3a --template worker --runtime-dir data/.tasks/launch-task/diag-worker-7f3a/ --task-file data/.tasks/launch-task/diag-worker-7f3a/task.md\n11. Run: tk close <the id of DIAG beta 7f3a> \"beta done 7f3a\"\n12. If you have a Skill tool, invoke the skill named build-app with it and stop as soon as it has loaded; otherwise run: cat .agents/skills/build-app/SKILL.md\nWhen all twelve items are done, reply with the single word: done."
  ],
  "timeout_seconds": 2400.0,
  "verification_timeout_seconds": 900.0,
  "mngr_branch": "main",
  "mngr_sha": "c1e4357690b44f5ad1b113fe2c289fe65a434144",
  "dwt_repo": "https://github.com/imbue-ai/default-workspace-template.git",
  "dwt_branch": "main",
  "dwt_sha": "d525d62ef9395dc4147dc736893be1d65481893a",
  "expectations": {
    "outcome": "The agent carried out the twelve items of the harness self-check, one tool call each.",
    "app_checks": [],
    "http_checks": [],
    "files_checks": [],
    "test_commands": [],
    "is_deliverable_bundle_required": false,
    "ui_flow_checks": [],
    "is_fresh_env_enabled": false,
    "max_judge_screenshots": null,
    "process_checks": [
      {
        "check_id": "skill_required_build_app",
        "kind": "required_skill",
        "skill": "build-app",
        "max_worker_launches": 0
      },
      {
        "check_id": "skill_required_diag_never_invoked_7f3a",
        "kind": "required_skill",
        "skill": "diag-never-invoked-7f3a",
        "max_worker_launches": 0
      },
      {
        "check_id": "skill_forbidden_diag_forbidden_7f3a",
        "kind": "forbidden_skill",
        "skill": "diag-forbidden-7f3a",
        "max_worker_launches": 0
      },
      {
        "check_id": "worker_launches",
        "kind": "max_worker_launches",
        "skill": "",
        "max_worker_launches": 1
      }
    ],
    "timing_checks": []
  },
  "authored_expectations": {
    "outcome": "The agent carried out the twelve items of the harness self-check, one tool call each.",
    "deliverable": null,
    "ui_flows": [],
    "test_commands": [],
    "is_fresh_env_enabled": false,
    "max_judge_screenshots": null,
    "process": {
      "required_skills": [
        "build-app",
        "diag-never-invoked-7f3a"
      ],
      "forbidden_skills": [
        "diag-forbidden-7f3a"
      ],
      "max_worker_launches": 1
    },
    "timing": null
  },
  "step": {
    "name": "work",
    "index": 0,
    "total": 2,
    "trial_lifetime_seconds": 8400.0,
    "entries_before": 0,
    "files": [],
    "is_diagnostic_probe_run": true,
    "seed_app": null
  }
}
```
