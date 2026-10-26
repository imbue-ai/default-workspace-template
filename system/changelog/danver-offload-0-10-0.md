- Upgraded offload from 0.9.14 to 0.10.0 on both pins the `test-offload` CI job
  keeps in lockstep: the runner's `OFFLOAD_VERSION` in `.github/workflows/ci.yml`
  and the one the image bakes in via `system/scripts/offload_sandbox_init.sh`.

- 0.10.0 identifies a cached base image by a content *recipe* (builder, copy
  dirs, `sandbox_init_cmd`, and digests of the inputs that escape the checkpoint
  tree) rather than by the path of the TOML config that asked for it. The three
  configs at the repo root -- `offload-modal.toml`, `offload-modal-chat.toml`,
  `offload-modal-system-interface.toml` -- are identical in every field that
  determines the image and differ only in pytest working directory, args and
  output dir, so under 0.9.14 each built and cached its own copy of the same
  image: three full base builds per job on a cold checkpoint, run one after
  another. Under 0.10.0 they resolve to one recipe, so the first suite builds and
  the other two reuse it.

- The note entry now also carries the recipe, the config path, the offload
  version and a build timestamp, so `git notes --ref refs/notes/offload-images
  show <sha>` says what an image was built from rather than only its id.

- The first run after this lands is cold by construction, twice over: bumping
  `offload_sandbox_init.sh` is itself a checkpoint build input, and 0.10.0 cannot
  match note entries written by 0.9.14 (they carry no recipe), so every existing
  cache entry misses once.
