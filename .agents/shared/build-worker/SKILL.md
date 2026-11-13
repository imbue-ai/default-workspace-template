---
name: build-worker
description: Do the work a chat agent handed off to you in the background -- build or change what your task file asks for, check it the way a user would, and report back. Read this in full when your task file names this guide.
metadata:
  author: imbue
  role: worker-sub-skill
---

# Doing handed-off work (build worker)

A chat agent (your lead) gave you part of the user's request so the user can
keep talking to it while you build. Your task file says what to build; this
guide says how to work and report so the lead can put your work in front of the
user as soon as it is usable.

## Step 1: Read your task file and get set up

Your task file was synced to your worktree, and its frontmatter names its own
path in `task_file`. Pass that exact path to the parser, which sets
`TASK_FILE`, `LEAD_AGENT`, `LEAD_WORK_DIR` and `FINISH_REPORT_PATH` for
reporting:

```bash
eval "$(uv run .agents/shared/scripts/parse_task_frontmatter.py <TASK_FILE>)"
```

Then read the whole task file. It is the only brief you get: whatever the lead
changes or adds later arrives as a message in your chat.

## Step 2: Load the references your task needs

Read each one that applies top to bottom, or only the sections its row names,
and use it as the last column says.
Filenames are relative to `.agents/shared/references/` unless a row gives a
full path.

| Load when | Reference | What it gives you |
|---|---|---|
| every run | `worker-reporting.md` | how to report back: the report procedure, milestones, never sleep, and the done and stuck bodies |
| every run | `user-facing-language.md` | the words to use in your step titles and report bodies, which the user reads |
| a message names a `REF-<id>` file | `element-references.md` | what a REF attachment holds and how to find the element it names |
| the task adds or changes a background service | `service-processes.md` | how a supervisord program is declared, added, changed and inspected |
| the task creates or restructures a skill | `spec-summary.md` | the skill basics. Read only "What a skill is" (without its subsections "The test: `[ai-script]` vs `[prose]`" and "Push prose to the edges"), "Directory layout" and "SKILL.md frontmatter". |
| the task changes an app | `.agents/shared/worker/references/type-app.md` | app information: what an app is, where its code, program file and `app.toml` references live. Ignore its testing and hardening instructions, and skip "Critical apps". |
| the task adds or changes a background service | `.agents/shared/worker/references/type-service.md` | service information: what a service is, where its code lives, and how to run it against scratch state. Ignore its testing and hardening instructions. |
| the task creates or changes a skill | `.agents/shared/worker/references/type-skill.md` | skill information: where a skill's behavior lives, and skills that drive an app. Read only "Where a skill's behavior lives" and "Skills that drive an app". |

## Step 3: Build

- Build to a usable state, and commit as you go. You may build all your
  requests together, in any order.
- Build only what the brief asks, for the case in front of you. Pick the
  simplest default for anything it leaves open, and name it in your report.
- Add a new dependency the normal way and commit the manifest changes, so they
  come with the merge.
- Do not harden (no new test suites or review gates) and do not hand off to
  another worker; hardening runs at turn end.
- Never request a secret: you have no chat to show a secret card in. Name the
  credential you need in your report, and your lead requests it.

Work only in your worktree. Until your lead merges you, never take the service
lease, restart the live app, write to its data, or run `workspace-layout open`,
`refresh` or `list` against the served tree.

## Step 4: Check your change

Check it the way your lead would in the chat; the user is not watching it live:

1. Run your own copy of the app, never the live one.
2. Do one real run of the main path.
3. Take a screenshot of each page you changed and look at it.

Failure cases, edge cases and regression checks belong to the hardening pass.
After committing, run the test gate once in the background, as a chat does, and
put the result in your report. `select-tests` only prints the commands; run
what it prints:

```bash
uv run app-manifest select-tests --diff-base <the commit before your work> > "/var/tmp/test-gate-$MNGR_AGENT_ID.sh"
python3 system/scripts/run_in_background.py --description "Run the test gate" -- bash "/var/tmp/test-gate-$MNGR_AGENT_ID.sh"
```

## Step 5: Report

- Declare a milestone as soon as a rough version works end to end, so your lead
  can show the user the shape early, and another each time a usable part is
  committed.
- Ending your turn to wait on a background command is safe: your lead counts
  you as busy until its report reaches you.
- Report `stuck` when you cannot make the main path work, saying why and where
  your work is.
