- `just minds-evals-view` serves a job directory with a vendored copy of harbor's viewer frontend,
  so eval results can eventually be rendered with annotations the stock viewer knows nothing about.
  The backend stays stock harbor: `create_app` takes the static directory as an argument, so only
  the pixels are ours.

- `apps/minds_evals/viewer/` is a verbatim copy of harbor's `apps/viewer` at `v0.22.0`, with
  `viewer/VENDORED_FROM.md` recording the tag and commit and `scripts/vendor_viewer.sh` re-vendoring
  at a given tag or reporting the local delta against one. The vendored tag has to match the harbor
  pin, since a newer frontend calls endpoints an older backend does not serve.

- The recipe builds on demand, bootstrapping a pinned bun into the project on first use: roughly ten
  seconds once, two after, and nothing when the build is current. Neither the toolchain nor the build
  output is committed, and the build is byte-identical to the bundle harbor ships in its own wheel.

- `viewer_contract_test.py` checks every endpoint URL in the vendored client against the routes
  harbor registers. Nothing in the toolchain otherwise connects the hand-written TypeScript to the
  Python, so a harbor upgrade that renamed a route would surface as a blank page rather than a failure.

- Upstream's `viewer/CLAUDE.md` is dropped on the way in, since Claude Code reads every `CLAUDE.md`
  beneath the checkout as project instructions and that one describes harbor's own repository.
  `scripts/vendor_viewer.sh` prunes it on every re-vendoring, so `--diff` against the pinned tag
  reports a clean tree.

- `just minds-evals-view` reads the viewer mode off the folder the way `harbor view` does, so a
  directory of task definitions browses as tasks rather than being served as an empty job list.
  `--mode` forces one, and the recipe's first parameter is now `folder` rather than `jobs`.

- The build uses whatever bun is already on `PATH` when it is new enough, and bootstraps one into
  the project only otherwise. The bootstrap no longer lets bun's installer append a `PATH` block to
  the developer's shell configuration or install its completions.
