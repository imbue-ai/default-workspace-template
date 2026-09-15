---
name: witness
description: Run `mngr witness` on a behavior corpus (setup, one mapper per .feature file, an adversarial reviewer per mapper, orchestrator integration, and an annealer) and land the deliverable branch as a PR. Covers invocation, watching a run through its manifest, judging the results, follow-up runs for files that failed, and cleanup. Invoke with /witness.
---

# Running mngr witness on a behavior corpus

This skill is the runbook for turning a behavior corpus (`<project>/behaviors/`) into witnessing tests with `mngr witness`. The corpus language itself is the `behaviors` skill; the command's own documentation is `libs/mngr/docs/commands/secondary/witness.md`; the design is `specs/behaviors-mapreduce/spec.md`.

## Step 0: Prerequisites

- No agent config is needed: agents launch as the plugin-provided `witness-claude` type, which is claude with permission prompts and startup dialogs switched off. Pass `--agent-type` only to use another type, and expect a warning if that type may pause.
- If a launch fails with `Source directory ... is not trusted by Claude Code`, trust the agent copy directory once (an ancestor is accepted): `uv run python -c "from pathlib import Path; from imbue.mngr_claude.claude_config import add_claude_trust_for_path; add_claude_trust_for_path(Path.home() / '.claude.json', Path.home() / '.mngr' / 'copies')"`. Say so before changing the user's config.
- Validate the corpus before spending agent time: `uv run mngr behaviors validate --root <project>/behaviors`. The command refuses a corpus with violations.
- A project may fill the prompt's project-fact blocks at `<project>/witness/mapper.j2` (also `scaffold.j2`, `reviewer.j2`, `annealer.j2`), each `{% extends %}`-ing the packaged template by name; it is discovered positionally. `libs/mngr_forward/witness/mapper.j2` is the exemplar.

## Step 1: Launch

Run from the repository root; the checkout's HEAD is the base commit every agent starts from, so commit anything the run should see first.

```bash
uv run mngr witness --root <project>/behaviors --provider local --output-dir /tmp/witness-<slug> > /tmp/witness-<slug>.log 2>&1 &
```

- `--feature <root-relative .feature path>` (repeatable) narrows the run to some files; `--area`, `--tag`, and `--unit` narrow units the way `mngr behaviors list` does.
- `--node-provider reduce=local` places one node elsewhere; `--node-env reduce:KEY=VALUE` gives one node's agents a variable (a push token for the annealer, never the mappers).
- `--max-running-agents` caps a node's live agents (six on local by default); `--timeout` is the per-agent wall clock (one hour by default).
- Expect tens of minutes per node on the local provider: a full corpus of eleven feature files took about an hour.

## Step 2: Watch the run

- `<output_dir>/execution.json` is rewritten whenever a node ends: each node's status and detail, and for agent nodes every job's slug, agent name, branch, archive location, error, and gate verdicts.
- Each job's archive is extracted under `<output_dir>/<node>/<agent name>/test_output/`: `outcome.json` (the agent's verdicts, changes, findings), `junit.xml`, and `witness_links.jsonl`.
- `uv run mngr list` shows the run's agents (`witness-<execution>-<node>-<slug>`); their branches are `witness/<execution>/<node>/<slug>` in the checkout.
- A launch failure such as `Timeout waiting for message submission evidence` means mngr could not confirm the prompt paste; the job is recorded as failed and the run continues while at least half of the node's jobs succeed. Rerun the affected files afterwards with `--feature`.

## Step 3: Judge the results

- The run exits non-zero when it halted (`stopped_reason` in the manifest). A map or review node halts only when fewer than half of its jobs succeeded; setup and reduce must succeed.
- For every mapper and reviewer outcome, check the verdicts: `FULL` must be backed by a trace naming every clause; `PARTIAL_STEADY` must list a genuinely open-world clause, not merely expensive residue; `blockers` and `behavior_problems` must be empty or understood. Reviewer `findings` say what the mapper got wrong and whether it was fixed.
- Compare coverage before and after with `uv run mngr behaviors matrix --root <project>/behaviors` on the base and on the deliverable branch; witness improvements must not lower it.

## Step 4: Ship the deliverable

- The deliverable is `witness/<execution>/reduce/integrated`, the integration branch plus the annealer's commits, ending in a `[CHANGELOG]` commit whose entries are named after the deliverable branch. Cherry-pick its commits onto a branch named after the PR (`git cherry-pick` keeps each `[KIND]` subject), rename the changelog entries to `<project>/changelog/<pr-branch>.md`, and drop anything no test uses (an empty `witnesses/` package skeleton, scaffolding the mappers never called).
- Run the touched projects' tests and `ruff format --check` on the branch, then `just test-offload` before opening the PR.

## Step 5: Clean up

- Destroy the run's agents (`uv run mngr destroy <names> --force` after a `--dry-run`), delete the `witness/<execution>/*` branches once the PR carries their commits, and keep `<output_dir>` if the review findings are still wanted.
