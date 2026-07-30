`minds launch-to-first-message` now refreshes default-workspace-template's `system/vendor/mngr` from the mngr SHA it is about to verify, before it freezes the template SHA.

The desktop binary runs the mngr SHA under test while the in-VM agent imports the template's vendored copy. The release runbook states that these must be equal -- a stale vendor silently rejects a field the binary renamed, so the agent never starts and the run wedges in a way that reads as a frontend hang -- but nothing enforced it, and dwt `main` drifts behind mngr `main` between releases. The scheduled run therefore verified a pair that was routinely skewed.

A new `sync_vendor` job archives the frozen mngr SHA into the template and pushes to its `main`, and `check_should_run` now freezes the template SHA afterwards, so the pair the run verifies is the pair that exists on both `main`s rather than a local edit the run throws away. `git archive` of a fixed SHA is byte-deterministic, so a run whose mngr SHA has not moved produces no commit, no push, and an unchanged pair key -- the skip-if-unchanged behavior is preserved exactly. A push rejected by a concurrent change re-derives from the new `main` and retries rather than rebasing, since replaying the same archive always yields the same tree.

Only the unattended `main`-vs-`main` run syncs. A dispatch naming `commit_sha`, `template_ref`, or `template_url` is verifying a specific pair and must never move the template's `main` onto that branch's mngr, so it reports the sync as skipped and changes nothing.

The push credential is an SSH deploy key scoped to default-workspace-template, read from Vault at `minds/release/DWT_VENDOR_SYNC_KEY_B64` under the existing `minds_release_gh` role, decoded to a mode-600 file and shredded in a step that always runs.
