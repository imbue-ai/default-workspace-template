Gitignores `witness_<execution>/`, the per-run output directory `mngr witness` writes into the repo
root by default. Each run left an untracked directory of archives and manifests behind, ready to be
swept into an unrelated commit.

This branch is stacked on the `mngr_file` behavior corpus branch and carries that branch's `dev`
change along with it: `mirror/copy.bara.sky` excludes
`libs/mngr_file/imbue/mngr_file/test_behavior_corpus.py` from the public mirror, alongside the
equivalent guard tests already excluded for `apps/minds` and `libs/mngr_forward`. That test
dev-depends on `imbue-mngr-behaviors`, which is internal-only and absent from the public tree. If
the corpus branch merges first, that part is already recorded there; it appears here because the
changelog gate evaluates each branch against `main` independently.
