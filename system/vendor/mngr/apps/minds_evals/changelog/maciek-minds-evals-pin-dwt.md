Pin the workspace template (`default-workspace-template`, "dwt") to an exact SHA, the way the mngr
source already was. Generation now resolves `dwt_branch` to a SHA, records it as `dwt_sha` in each
task's `[metadata]` (alongside the branch it was resolved from) and in the case config, and the box
clones that SHA onto a real local branch instead of running `git clone --branch <branch>` at trial
time. Each trial's own record (`state.json` and the agent metadata) now carries `mngr_sha` and
`dwt_sha`, so a captured trial says which mngr and which template produced it.

Behavior change: `dwt_branch: main` used to mean "whatever main is when the trial runs"; it now
means "main as of generation time". Two runs of the same dataset a week apart build identical
workspaces, and picking up new template changes requires regenerating the dataset. Datasets
generated before this change carry no `dwt_sha` and are rejected at trial start -- regenerate them.

Generation now reaches the dwt remote as well as the mngr remote, so it needs read access to both.
