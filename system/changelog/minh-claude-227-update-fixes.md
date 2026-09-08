Regenerate `uv.lock`, which had drifted out of sync with `pyproject.toml`.

This is not a change to the workspace's dependencies so much as a repair of the record of them. The `Check uv.lock matches pyproject` CI step was failing on `main` itself and on every open PR, so the signal it exists to give had been dead long enough that a red run no longer told anyone anything. Running `uv lock` brings the file back in line and the step green.

Two things are in the diff. Most of it is marker normalisation on `browser-use-core`'s dependencies, where a long platform-marker expression collapses to nothing because it is now implied. The one substantive entry is `sentry-sdk`, whose floor had been raised from `>=2.59.0` to `>=2.63.0` in `pyproject.toml` without a corresponding relock -- which is the drift that broke the gate in the first place.

It rides along with an unrelated model-catalog change rather than landing on its own because the gate blocks that change from going green, and a repo-wide red CI step is worth more fixed than tidily scoped.
