Workspace plumbing for the new `apps/minds_evals` harbor app:

- harbor 0.21.0 is pinned via the upstream git tag (the PyPI release postdates the supply-chain cooldown cutoff), with `override-dependencies = ["rich>=13.9.4,<14.0"]` at the root because harbor's rich floor can never co-resolve with litellm[proxy]'s cap; the workspace keeps rich 13.x.

- New `just minds-evals-generate` and `just minds-evals-run` recipes (private.just); the run recipe uploads nothing, so results stay in the local job dir.

- test_meta_ratchets' per-project test_ratchets.py discovery now respects .gitignore, so generated eval datasets (which embed a full mngr-internal clone in a gitignored directory) no longer break the meta ratchets on machines that have generated one.

- The root and public-mirror-overlay `uv.lock` files, plus two `image_requirements.txt` exports, are refreshed for the filelock/platformdirs transitive bumps the harbor dependency pulls forward. Harbor task templates are omitted from repo-wide coverage.
