# Nightly flake sweep in GitHub CI

## Overview

* Today the full-window flake sweep only runs when a human remembers to run `/detect-flakes`. Because a full sweep is the *only* path permitted to close stale tickets, skipping days means the MIND backlog silently drifts — open tickets for flakes that stopped happening, and no ticket for flakes that started.
* Run the existing skills unchanged in spirit: `scripts/flake_reconcile.py` moves data and never clusters, so Claude Code runs headlessly on the GitHub runner and supplies the clustering, prioritization and reconciliation judgment. No attempt is made to reimplement that judgment deterministically.
* The skills gain an explicit `--autonomous` mode that **only auto-advances the approval gate**. The architecture, and particularly the step where the plan is narrated before it is applied, stay exactly as they are — narrating the plan measurably improves compliance, so the unattended run still writes the plan first and then applies it.
* Linear access reuses latchkey rather than modifying `flake_reconcile.py`, and every credential comes from Vault via GitHub OIDC, matching how the rest of CI already gets secrets.
* The workflow owns its own setup steps instead of depending on `.github/actions/tmr-setup`, accepting a little duplication to stay insulated from TMR's churn.

## Expected behavior

* The workflow runs daily at 13:00 UTC — clear of TMR's 08:00 UTC window and its 4-hour worst case — and can also be triggered manually with no inputs, so a manual run is always identical to a scheduled one.
* It checks out `main` with full history, installs the Python toolchain, project dependencies and the Claude Code CLI, pulls secrets from Vault, and authenticates latchkey to Linear non-interactively.
* Before any model time is spent, it preflights both credentials: latchkey must report Linear valid and `gh` must be able to read check-runs. A missing or wrong credential fails in seconds with a clear message rather than deep inside a long run.
* Claude then runs `/detect-flakes --autonomous`. The sweep reads the default 14-day window across the default suites, exactly as an interactive run would.
* `manage-flakes` behaves identically to the interactive path except that it does not stop for approval: it writes the intended CREATE / UPDATE / CLOSE / state-change plan out, applies it, then records what actually changed.
* Tickets are created, updated and closed with no human in the loop. All existing safety rules still hold — closes remain restricted to full-window sweeps whose cluster is absent and whose `last-seen` is older than 21 days, and tickets in In Progress / Done / Canceled are still never demoted or reopened.
* Full git history is available, so the skill can keep scoping evidence to post-fix commits with `git merge-base --is-ancestor`, as the current MIND-209/210/212 ticket bodies do.
* Every run posts the same markdown summary to both the GitHub job summary and Slack — including a run that changed nothing, which is the point: the job doubles as a daily heartbeat, so silence means breakage.
* If the run fails, or the summary file is missing, Slack still receives a failure notice linking the run, and the job goes red. A heartbeat that goes quiet exactly when something breaks would be worse than none.
* The summary plus the sweep's intermediates are uploaded as a retained artifact, so a bad night stays diagnosable after the fact.
* Two scheduled runs never overlap; a second firing queues behind the first rather than cancelling it.
* Interactive behavior is unchanged. Without `--autonomous`, both skills still stop and wait for approval. `report-incidental-flakes` is untouched: it stays interactive and remains create/update-only, never closing.
* A repeated sweep over a quiet window still produces no ticket changes, so the nightly job is idempotent and a re-run after a failure converges rather than duplicating work.

## Changes

* Add a new scheduled workflow that owns the whole job: daily cron plus manual dispatch, a concurrency group that queues rather than cancels, a 120-minute timeout, and Opus as the model.
* Give the workflow read-only repository permissions plus the ability to read CI: it needs to read workflow runs and check-runs, and to mint an OIDC token for Vault. It writes nothing to the repository.
* Duplicate the small amount of runner setup it needs — Python, uv, `uv sync --all-packages`, the pinned Claude Code CLI, git identity, and pre-trusting the checkout so Claude does not block on its trust dialog — rather than calling the TMR setup action.
* Install latchkey in CI at an explicit pinned version and authenticate it to Linear from the Vault-supplied token.
* Extend the `bump-latchkey` skill to cover this new CI pin, which becomes a fifth pin location alongside the four it already coordinates.
* Teach `detect-flakes` to accept `--autonomous`, to state that the set is a full-window sweep *and* that the run is unattended when it hands off, and to require that the summary file be produced.
* Teach `manage-flakes` to accept `--autonomous` directly as well, so it can be driven standalone by future automation. When set, it auto-advances past the approval gate; when unset, its behavior is exactly as today.
* Define the summary as a single markdown file at a fixed repo-root path, written by the skill and consumed verbatim by both the job summary and Slack. Its absence is treated as a failed run.
* Add that summary file to `.gitignore`, since the run materializes it into the working tree.
* Upload the summary and the sweep's JSON intermediates as a retained build artifact.
* Add a Slack notification step driven by a new Vault-held webhook, which warns and skips when the webhook is absent rather than failing — matching the existing Slack precedent in CI.
* Record the two new Vault entries as an operational prerequisite; the plan documents them rather than automating their creation.
* Leave `scripts/flake_reconcile.py` completely unchanged — the latchkey route means no code change is needed for Linear.
* Nothing is needed for the public mirror: its config is an allowlist, and none of these paths are on it, so the workflow, the skills and the summary all stay private automatically.
* Add the `dev/` changelog entry this branch owes, since the change touches root-level `.github/` and `.claude/` files.
* Temporarily, and only until merge: a branch-scoped `push:` trigger that runs Claude **without** `--autonomous`, so the skill stops at its approval gate and writes nothing while still proving checkout, Claude, Vault, latchkey→Linear and `gh`→check-runs all work. Removed before the workflow lands with cron and dispatch live.

## Rollout

* `workflow_dispatch` and `schedule` only take effect for workflow files that already exist on the default branch, so a brand-new workflow cannot be dispatched from its PR. The temporary `push:` trigger is what makes pre-merge validation possible at all.
* Sequence: open the PR with the temporary trigger → confirm the read-only shakeout goes green → delete the temporary trigger → merge with cron and dispatch live → dispatch once from `main` for the first real write run → review the Slack post → let the cron take over.

## Accesses you need to provision

**This is the list you asked me to surface at the end — none of it is code, and nothing works until it exists.**

### In Linear

1. **Create a dedicated bot / service account** for the sweep (not a personal API key), so nightly writes are visibly machine-authored.
2. **Add that account to the `MIND` team**, with permission to:
   * read issues, teams, workflow states and labels (the CLI resolves team and state IDs on every run);
   * **create** issues;
   * **update** issues — body and title;
   * **change workflow state**, both to Todo/Backlog and to a completed state (this is what `close-ticket` and `set-status` need);
   * **create comments** (every state change leaves one explaining why);
   * **create labels** — the CLI creates a flaky-cluster label on first use, so a read-only label scope is not enough.
3. **Generate an API key** for that account.

### In Vault

4. Store that key at **`mngr/ci/LINEAR_FLAKE_SWEEP_API_KEY`**, readable by role **`mngr_ci_gh`**.
5. Store a Slack incoming webhook at **`mngr/ci/SLACK_FLAKE_SWEEP_WEBHOOK`**, same role.
   * Target channel: **`#minds-notifications`** — despite the name it is the repo's de facto CI
     notification channel, already receiving the daily `launch-to-msg` heartbeat from the
     `minds-macrunner` app. Add the new webhook under that same Slack app.
   * Caveat: the sweep covers all of `mngr-internal`, not just minds, so the channel name
     under-sells the scope. A dedicated `#engineering-flakes` is the alternative if the daily
     heartbeat proves noisy alongside `launch-to-msg`.
6. Confirm **`mngr_ci_gh`** is assumable from this repository's OIDC token and is **not branch-restricted** — the pre-merge shakeout runs from a feature branch. TMR already fetches Vault secrets on branch dispatches, which suggests this is fine, but it is worth confirming before relying on it.

#### Pushing the values

The KV layout stores each key as its own single-`value` leaf under the `secrets`
mount, so the key name is the last path segment — the same shape
`scripts/push_vault_from_file.py` writes.

```bash
export VAULT_ADDR=https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200
export VAULT_NAMESPACE=admin
vault login -method=oidc          # if not already logged in

# Confirm the mount and path are what we expect before writing:
vault kv list -mount=secrets mngr/ci      # should list ANTHROPIC_API_KEY, MODAL_TOKEN_ID, ...

# Read the token without putting it in shell history, then write it as JSON on
# stdin. Do NOT pass `value=$TOKEN` positionally: the vault CLI treats a leading
# `@` in a positional value as a read-from-file sigil. `read -rs` (no `-p`) and the
# inline SECRET= export keep this working under both bash and zsh.
printf 'Linear API key: '; read -rs SECRET; echo
SECRET="$SECRET" python3 -c 'import json,os,sys; sys.stdout.write(json.dumps({"value": os.environ["SECRET"]}))' \
  | vault kv put -mount=secrets mngr/ci/LINEAR_FLAKE_SWEEP_API_KEY -
unset SECRET

# Same for the Slack webhook:
printf 'Slack webhook URL: '; read -rs SECRET; echo
SECRET="$SECRET" python3 -c 'import json,os,sys; sys.stdout.write(json.dumps({"value": os.environ["SECRET"]}))' \
  | vault kv put -mount=secrets mngr/ci/SLACK_FLAKE_SWEEP_WEBHOOK -
unset SECRET

# Verify both round-trip:
vault kv get -field=value -mount=secrets mngr/ci/LINEAR_FLAKE_SWEEP_API_KEY
vault kv get -field=value -mount=secrets mngr/ci/SLACK_FLAKE_SWEEP_WEBHOOK
```

### Note on blast radius

The bot's token can close tickets. That is intended — closing stale tickets is the whole reason a full-window sweep exists — but it does mean the credential should belong to a service account scoped to the `MIND` team and nothing else.
