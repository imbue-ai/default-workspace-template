# mngr-minds-eval

`minds-evals` — run persona-based evals against Minds on Modal. Each case is a self-completing Modal
workspace that drives its own multi-turn chat, snapshots `/mngr` to R2 each turn, and uploads its
transcript. Nothing runs on your machine; the CLI only makes API calls. A **box** is a full Minds
computer (Modal sandbox) built from an exact mngr SHA; it creates the workspaces and streams a
desktop URL you can watch. Results read back from R2, so your machine need not stay on.

## Setup (one-time)

```
./setup-r2.sh                        # R2 bucket + scoped key -> ~/.minds-eval/r2.env  (see SETUP.md)
export ANTHROPIC_API_KEY=sk-ant-...
```
Also required: `gh auth login` (mngr-internal access — the box pulls mngr fresh) and `modal token new`.

## Commands

| command | what it does |
|---|---|
| `launch <name> --config <f>` | run a batch: one self-completing workspace per case. `<name>` is unique (R2 prefix + Modal env). |
| `list-batches` | list batches (R2 read, no box). |
| `inspect <name>` | per-case status (R2 read, no box). |
| `evaluate <name>` | score finished cases, write results to R2 (needs `ANTHROPIC_API_KEY`). |
| `visit-batch <name>` | rebuild the exact box the batch ran on; prints a desktop URL. |
| `stop <name>` | terminate a batch's box (its workspaces live on). |
| `box --mngr-branch <b>` | dev utility: boot a desktop box on a branch tip (optionally make one workspace). |

### launch
```
minds-evals launch smoke --config eval-config-small.json
```

### box — bare (just the desktop computer, no workspace)
```
minds-evals box --mngr-branch main --user-id me
```

### box — with a workspace (`--dwt-repo`; needs `ANTHROPIC_API_KEY`)
```
minds-evals box --mngr-branch main --user-id me \
  --dwt-repo https://github.com/imbue-ai/default-workspace-template.git \
  --dwt-branch main --workspace-name ws
```
Add `--vendor-box-mngr` so the workspace's committed `vendor/mngr` is overwritten with the box's mngr
(`--mngr-branch` then governs both the backend and the workspace's internal mngr).

### Env / feature flags — two levels
Both flags take variable **NAMES**; the value comes from your shell (pre-set it), and an unset name
errors immediately.
```
export FLAG=1
minds-evals box --mngr-branch b --user-id me --box-env FLAG                        # -> box container (minds app)
minds-evals box --mngr-branch b --user-id me --dwt-repo <repo> --dwt-branch x \
    --workspace-env FLAG                                                            # -> the workspace's agent env
minds-evals launch n --config c.json --box-env FLAG_A --workspace-env FLAG_B        # both, applied to every case
```
- `--box-env NAME` → the box's mngr/minds container. Works on any `--mngr-branch`. (repeatable)
- `--workspace-env NAME` → each created workspace's host env. Needs `--dwt-repo` (there is no
  workspace otherwise), and the box's mngr must carry the `--pass-host-env` support (a
  `minds-evals-desktop`-based branch). (repeatable)
- **Reuse caveat:** a box with the same `--user-id` + branch tip is reused with its ORIGINAL env — use
  a fresh `--user-id` (or `stop` the old box) to apply changed flags.

## Eval config (`--config`)

A reusable template; the batch `<name>` is given on the command line, not in the file.
```json
{
  "mngr_branch": "minds-evals-desktop",
  "timeout_seconds": 3600,
  "personas": [
    {"id": "todo-app", "persona": "...", "prompts": ["Build me ...", "Sounds good.", "DECIDE_FROM_PERSONA"]}
  ]
}
```
- `mngr_branch` — the box's mngr (governs the compute + which minds features the box runs).
- `dwt_branch`/`dwt_repo` — optional; default the default-workspace-template `main` branch, which
  carries the eval worker. The branch must carry the worker or the sandbox boots but never self-runs.
- `timeout_seconds` — optional (default 3600 = 1h): per-case wall-clock budget; a run past it self-terminates.
- Each `prompts` entry is one turn: a **literal** string sent verbatim, or **`DECIDE_FROM_PERSONA`**
  (the worker role-plays the client via the Anthropic API; cannot be the first entry).

## R2 layout

```
<name>/                              batch (the unique eval name)
  config.json                        config verbatim + created_at/restic_password/mngr_sha/modal_user_id/modal_env
  <name>_<case_id>/
    state.json                       per-turn status (test_state: ongoing | finished | timed_out)
    artifacts/full_transcript.jsonl  written on the final turn
    restic/                          the case's restic repo (tagged /mngr snapshots)
```

## Notes

- `evaluate` scores only **finished** cases (avg words/turn + three 1-10 LLM scores); running/timed-out
  cases show `N/A`. Results land in `<case>/case_eval_results.json` + `<batch>/batch_eval_results.json`.
- Boxes self-terminate after 8h; `stop <name>` kills one early. Modal envs accumulate one per batch —
  wipe with `TERM=dumb uv run python scripts/modal_nuke.py -e <modal_env> --force`.
- The eval worker no-ops unless `system/services/eval_worker/test_case_metadata.json` is present,
  so normal workspaces are unaffected.
