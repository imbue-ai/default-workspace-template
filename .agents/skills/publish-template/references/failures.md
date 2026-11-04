# When a publish step fails

Every failure here is "fix the input and retry the step", or "stop and tell the
user what failed and that nothing was published". It is never "publish something
smaller": no hand-assembled subset, no `gh api` file upload, no `git init` of
just the app, no force-push. A template that does not boot is worse than no
template.

## The assembly script (`build_template.sh`, reported by the worker as `stuck`)

| Exit | Meaning | What to do |
|---|---|---|
| 1 | The secret scan found a credential in the included paths, or a scanner (betterleaks, kingfisher) is missing or errored. | A finding: tell the user which file (never the value) and stop, or drop/generalize the path and relaunch. A scanner problem: the environment is broken -- the stderr names the install command; never publish around it. |
| 2 | Usage error, or the script refused to run in the live workspace or a main checkout. | Fix the arguments; assembly only ever runs in the worker's throwaway worktree. |
| 3 | The include set adds nothing beyond `BASE_REF`. | Tell the user plainly; there are no empty template repos. |
| 4 | The base's `system/supervisord.conf` does not parse, or realizes no programs at all. | The base is wrong: re-resolve `BASE_REF` and relaunch. (An included app with unmet requirements is expected and does not fail this.) |
| 5 | `BASE_REF` is not a bootable template tree (no `pyproject.toml`, `system/supervisord.conf` or `system/supervisord.conf.d`), or it descends from this workspace's `Initial workspace commit`. | Re-resolve with `resolve_template_base.py` (for an `update-self:` merge the base is its second parent) and relaunch. |
| 6 | The generated manifest did not validate, or the secrets check failed: an included program or MCP server runs under a `data/.secrets/<file>.env` no included `app.toml` or `SKILL.md` declares, or a declared variable is missing from this workspace's own file. | Fix the declaration at its source (the `connect-external-service` skill documents the shapes), commit, and relaunch. |

## The validator (`validate_template.py`)

It lists every problem at once: fix them together, commit in `$WT`, re-run. A
declared apt package that does not resolve means it is not in the pinned
mirror -- ship it as an `env.d` unit instead, or drop it.

## Checking GitHub access (`github_access.py`)

- `UNKNOWN (...)`, exit 2: the gateway could not say whether the grant is there.
  File nothing while any kind is UNKNOWN. The reason in the parentheses says why:
  - `Desktop ... is unreachable` or `No desktop has announced itself to this
    machine`: the user's Imbue Studio app is closed or asleep, or has not
    connected to this machine yet. Whether a grant is missing cannot be told
    until the app answers, and approving one needs the app anyway: ask the user
    to open the app, then check again.
  - Anything else (`latchkey exited ...`, `unreadable answer: ...`, or another
    error from the gateway): the gateway did not answer properly. Check again;
    if it persists, stop and tell the user GitHub access could not be checked.

## Creating the repo (`github_repo.py create`)

- `request not permitted by the user`: the `github-rest-api` grant is missing or
  too narrow (repo creation needs `github-write-all`; `github-write-repos` does
  not cover `POST /user/repos`). Request it again.
- `name already exists`: ask the user for another name and re-run `create`.
- A `follow-up:` line about the lockdown: the repo exists and stays private unless
  the user chose public; carry on and mention it at the end.
- A `follow-up:` line saying README.md is missing or carries no
  `MINDS_TEMPLATE_REPO_URL`: the README, or its Open in Imbue Studio link's
  placeholder, is gone, so nothing was pointed at the new repo. Restore the
  README or fix the link in `$WT` and commit before the push.
- `was created, but committing README.md ... failed`: the repo exists (its
  `<owner>/<repo>` is still printed) and README.md already points at it. Commit
  README.md in `$WT` and go on to the push; never re-run `create`.

## Pushing (`push_snapshot.py`)

Never re-create the repo to retry a push. The script names the likely cause:

- `... is a repo's main worktree` or `... does not exist`: `--worktree` was not
  `$WT` (an unset `$WT` names the cwd, usually the live workspace; a stale one
  names a removed worktree). Re-resolve `$WT` from the worker's `done` report and
  run the push again with it.
- `--parent ... is not a commit`: the value was empty or wrong (an unset
  `$BASE_REF`, a mistyped sha). Re-resolve it and run the push again.
- the published `main` moved (non-fast-forward): someone else pushed there. Stop
  and tell the user; never force.
- `github-git-write` missing: request it again.
- HTTP 413: the user's Imbue Studio app is older than the gateway's body cap and
  cannot proxy a push this size. Tell the user and stop.
- `workflow` scope: the stored GitHub credential cannot push `.github/workflows/`
  files. Tell the user and stop; never strip the files.
- A `follow-up:` line about the tag: the version is published; mention it.

If the push failed and you stop, leave the worker, `$WT` and its branch in place
so the user can retry, and record nothing in the ledger.
