# Minds persona eval: instrument -- step 1 of 1 (instrument)

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

Persona: A client who only wants an acknowledgement.

The workspace is created with the app `todo-fixture` seeded in: before it exists, the driver commits `/work/step_seeds/instrument` to `system/fixtures/todo-fixture/` on the pinned template, with a supervisord program serving it on loopback port 8090, and merges that commit onto the case base. A seed that conflicts with the case base, or registers on top of something the template runs, ends the trial before any workspace is created.

This step introduces no files into the workspace.

This step's entries. A literal message is sent verbatim; `DECIDE_FROM_PERSONA`
is a message the persona model writes in character; a goal entry is not a
message at all, but a stretch of conversation the client model keeps going,
within its exchange budget, until it says the goal is met:

1. Reply with the single word: acknowledged. Do not do anything else.
2. (goal, up to 1 exchange(s)) the client keeps the conversation going until it is satisfied that: The assistant has replied with the word acknowledged.

This step is graded against: The workspace serves the seeded to-do fixture unchanged.

The machine-readable case config for this step. This fenced block is the only
part of the file the driver reads:

```json
{
  "case_id": "instrument",
  "persona": "A client who only wants an acknowledgement.",
  "prompts": [
    "Reply with the single word: acknowledged. Do not do anything else.",
    {
      "goal": "The assistant has replied with the word acknowledged.",
      "max_exchanges": 1
    }
  ],
  "timeout_seconds": 1800.0,
  "verification_timeout_seconds": 1800.0,
  "mngr_branch": "main",
  "mngr_sha": "c1e4357690b44f5ad1b113fe2c289fe65a434144",
  "dwt_repo": "https://github.com/imbue-ai/default-workspace-template.git",
  "dwt_branch": "main",
  "dwt_sha": "d525d62ef9395dc4147dc736893be1d65481893a",
  "expectations": {
    "outcome": "The workspace serves the seeded to-do fixture unchanged.",
    "app_checks": [
      {
        "check_id": "app_registered",
        "min_registered_apps": 0,
        "is_supervisord_service_required": true
      }
    ],
    "http_checks": [
      {
        "check_id": "http_0_registered_apps",
        "target": "registered-apps",
        "expect_status": 200,
        "expect_body_regex": ""
      },
      {
        "check_id": "http_1_todo_fixture",
        "target": "todo-fixture",
        "expect_status": 200,
        "expect_body_regex": "<title>Todo</title>"
      },
      {
        "check_id": "http_2_todo_fixture",
        "target": "todo-fixture",
        "expect_status": 200,
        "expect_body_regex": "DIAG-absent-7f3a"
      }
    ],
    "files_checks": [
      {
        "check_id": "files_0",
        "glob": "workspace/system/fixtures/todo-fixture/index.html",
        "min_count": 1
      },
      {
        "check_id": "files_1",
        "glob": "workspace/system/fixtures/todo-fixture/absent-7f3a.html",
        "min_count": 1
      }
    ],
    "test_commands": [
      "test -s system/fixtures/todo-fixture/index.html",
      "test -e system/fixtures/todo-fixture/absent-7f3a.html"
    ],
    "is_deliverable_bundle_required": true,
    "ui_flow_checks": [
      {
        "check_id": "ui_flow_0_plain",
        "name": "plain",
        "actions": "Step 1: type 'walk dog' into the textbox named 'New task'. Step 2: click the button named 'Add'. Step 3: click the checkbox named 'walk dog'. Step 4: click the button named 'Delete \"walk dog\"'. Step 5: click the button named 'Refresh'. Step 6: reload the page.",
        "script": [
          {
            "kind": "input",
            "role": "textbox",
            "target": "New task",
            "beside": "",
            "text": "walk dog",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Add",
            "beside": "",
            "text": "",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "checkbox",
            "target": "walk dog",
            "beside": "",
            "text": "",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Delete \"walk dog\"",
            "beside": "",
            "text": "",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Refresh",
            "beside": "",
            "text": "",
            "amount": 0
          },
          {
            "kind": "reload",
            "role": "",
            "target": "",
            "beside": "",
            "text": "",
            "amount": 0
          }
        ],
        "expect": "'walk dog' was added, completed and deleted; the seed tasks are untouched.",
        "surface": "origin",
        "start_path": ""
      },
      {
        "check_id": "ui_flow_1_deferred",
        "name": "deferred",
        "actions": "Step 1: type 'walk dog' into the textbox named 'New task'. Step 2: click the button named 'Add'.",
        "script": [
          {
            "kind": "input",
            "role": "textbox",
            "target": "New task",
            "beside": "",
            "text": "walk dog",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Add",
            "beside": "",
            "text": "",
            "amount": 0
          }
        ],
        "expect": "'walk dog' is listed.",
        "surface": "origin",
        "start_path": "?latency=300"
      },
      {
        "check_id": "ui_flow_2_armed_delete",
        "name": "armed-delete",
        "actions": "Step 1: click the button named 'Delete \"Buy milk\"'. Step 2: click the button named 'Delete \"Buy milk\"'.",
        "script": [
          {
            "kind": "click",
            "role": "button",
            "target": "Delete \"Buy milk\"",
            "beside": "",
            "text": "",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Delete \"Buy milk\"",
            "beside": "",
            "text": "",
            "amount": 0
          }
        ],
        "expect": "'Buy milk' is gone.",
        "surface": "origin",
        "start_path": "?arm_delete=1"
      },
      {
        "check_id": "ui_flow_3_pending",
        "name": "pending",
        "actions": "Step 1: type 'walk dog' into the textbox named 'New task'. Step 2: click the button named 'Add'. Step 3: wait for the page to change.",
        "script": [
          {
            "kind": "input",
            "role": "textbox",
            "target": "New task",
            "beside": "",
            "text": "walk dog",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Add",
            "beside": "",
            "text": "",
            "amount": 0
          },
          {
            "kind": "wait",
            "role": "",
            "target": "",
            "beside": "",
            "text": "",
            "amount": 0
          }
        ],
        "expect": "'walk dog' is listed once saving finishes.",
        "surface": "origin",
        "start_path": "?pending=2000"
      },
      {
        "check_id": "ui_flow_4_ticker",
        "name": "ticker",
        "actions": "Step 1: click the button named 'Refresh'.",
        "script": [
          {
            "kind": "click",
            "role": "button",
            "target": "Refresh",
            "beside": "",
            "text": "",
            "amount": 0
          }
        ],
        "expect": "Nothing but the clock changes.",
        "surface": "origin",
        "start_path": "?ticker=1"
      },
      {
        "check_id": "ui_flow_5_start_over",
        "name": "start-over",
        "actions": "Step 1: click the link named 'Start over'.",
        "script": [
          {
            "kind": "click",
            "role": "link",
            "target": "Start over",
            "beside": "",
            "text": "",
            "amount": 0
          }
        ],
        "expect": "The page reloads at its own root.",
        "surface": "origin",
        "start_path": "?latency=300"
      },
      {
        "check_id": "ui_flow_6_unnamed",
        "name": "unnamed",
        "actions": "Step 1: type 'walk dog' into the textbox named 'New task'. Step 2: click the button named 'Add'. Step 3: click the checkbox that has no accessible name beside 'walk dog'. Step 4: click the button named 'Delete \"walk dog\"'.",
        "script": [
          {
            "kind": "input",
            "role": "textbox",
            "target": "New task",
            "beside": "",
            "text": "walk dog",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Add",
            "beside": "",
            "text": "",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "checkbox",
            "target": "",
            "beside": "walk dog",
            "text": "",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Delete \"walk dog\"",
            "beside": "",
            "text": "",
            "amount": 0
          }
        ],
        "expect": "'walk dog' was added, completed through its nameless checkbox and deleted; the seed tasks are untouched.",
        "surface": "origin",
        "start_path": "?unnamed=1"
      }
    ],
    "is_fresh_env_enabled": false,
    "max_judge_screenshots": null,
    "process_checks": [],
    "timing_checks": [
      {
        "fast_seconds": 600.0,
        "slow_seconds": 1500.0,
        "requires_no_failures": [
          "http"
        ],
        "check_id": "time_to_goal"
      }
    ]
  },
  "authored_expectations": {
    "outcome": "The workspace serves the seeded to-do fixture unchanged.",
    "deliverable": {
      "kind": "MINDS_APP",
      "min_registered_apps": 0,
      "http": [
        {
          "target": "todo-fixture",
          "expect_status": 200,
          "expect_body_regex": "<title>Todo</title>"
        },
        {
          "target": "todo-fixture",
          "expect_status": 200,
          "expect_body_regex": "DIAG-absent-7f3a"
        }
      ],
      "files": [
        {
          "glob": "workspace/system/fixtures/todo-fixture/index.html",
          "min_count": 1
        },
        {
          "glob": "workspace/system/fixtures/todo-fixture/absent-7f3a.html",
          "min_count": 1
        }
      ]
    },
    "ui_flows": [
      {
        "name": "plain",
        "actions": "",
        "expect": "'walk dog' was added, completed and deleted; the seed tasks are untouched.",
        "script": [
          {
            "kind": "input",
            "role": "textbox",
            "target": "New task",
            "beside": "",
            "text": "walk dog",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Add",
            "beside": "",
            "text": "",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "checkbox",
            "target": "walk dog",
            "beside": "",
            "text": "",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Delete \"walk dog\"",
            "beside": "",
            "text": "",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Refresh",
            "beside": "",
            "text": "",
            "amount": 0
          },
          {
            "kind": "reload",
            "role": "",
            "target": "",
            "beside": "",
            "text": "",
            "amount": 0
          }
        ],
        "surface": "origin",
        "start_path": ""
      },
      {
        "name": "deferred",
        "actions": "",
        "expect": "'walk dog' is listed.",
        "script": [
          {
            "kind": "input",
            "role": "textbox",
            "target": "New task",
            "beside": "",
            "text": "walk dog",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Add",
            "beside": "",
            "text": "",
            "amount": 0
          }
        ],
        "surface": "origin",
        "start_path": "?latency=300"
      },
      {
        "name": "armed-delete",
        "actions": "",
        "expect": "'Buy milk' is gone.",
        "script": [
          {
            "kind": "click",
            "role": "button",
            "target": "Delete \"Buy milk\"",
            "beside": "",
            "text": "",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Delete \"Buy milk\"",
            "beside": "",
            "text": "",
            "amount": 0
          }
        ],
        "surface": "origin",
        "start_path": "?arm_delete=1"
      },
      {
        "name": "pending",
        "actions": "",
        "expect": "'walk dog' is listed once saving finishes.",
        "script": [
          {
            "kind": "input",
            "role": "textbox",
            "target": "New task",
            "beside": "",
            "text": "walk dog",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Add",
            "beside": "",
            "text": "",
            "amount": 0
          },
          {
            "kind": "wait",
            "role": "",
            "target": "",
            "beside": "",
            "text": "",
            "amount": 0
          }
        ],
        "surface": "origin",
        "start_path": "?pending=2000"
      },
      {
        "name": "ticker",
        "actions": "",
        "expect": "Nothing but the clock changes.",
        "script": [
          {
            "kind": "click",
            "role": "button",
            "target": "Refresh",
            "beside": "",
            "text": "",
            "amount": 0
          }
        ],
        "surface": "origin",
        "start_path": "?ticker=1"
      },
      {
        "name": "start-over",
        "actions": "",
        "expect": "The page reloads at its own root.",
        "script": [
          {
            "kind": "click",
            "role": "link",
            "target": "Start over",
            "beside": "",
            "text": "",
            "amount": 0
          }
        ],
        "surface": "origin",
        "start_path": "?latency=300"
      },
      {
        "name": "unnamed",
        "actions": "",
        "expect": "'walk dog' was added, completed through its nameless checkbox and deleted; the seed tasks are untouched.",
        "script": [
          {
            "kind": "input",
            "role": "textbox",
            "target": "New task",
            "beside": "",
            "text": "walk dog",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Add",
            "beside": "",
            "text": "",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "checkbox",
            "target": "",
            "beside": "walk dog",
            "text": "",
            "amount": 0
          },
          {
            "kind": "click",
            "role": "button",
            "target": "Delete \"walk dog\"",
            "beside": "",
            "text": "",
            "amount": 0
          }
        ],
        "surface": "origin",
        "start_path": "?unnamed=1"
      }
    ],
    "test_commands": [
      "test -s system/fixtures/todo-fixture/index.html",
      "test -e system/fixtures/todo-fixture/absent-7f3a.html"
    ],
    "is_fresh_env_enabled": false,
    "max_judge_screenshots": null,
    "process": null,
    "timing": {
      "fast_seconds": 600.0,
      "slow_seconds": 1500.0,
      "requires_no_failures": [
        "http"
      ]
    }
  },
  "step": {
    "name": "instrument",
    "index": 0,
    "total": 1,
    "trial_lifetime_seconds": 4500.0,
    "entries_before": 0,
    "files": [],
    "is_diagnostic_probe_run": false,
    "seed_app": {
      "name": "todo-fixture",
      "port": 8090,
      "box_path": "/work/step_seeds/instrument"
    }
  }
}
```
