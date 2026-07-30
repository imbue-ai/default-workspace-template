`libs/mngr` gains a `python-frontmatter` dependency, used to read the `name:` frontmatter of
output-style files when resolving `mngr create --output-style`. The root lockfile is updated
to match; no build or CI tooling changes.

`mirror/overlay/uv.lock` regenerated for the new `python-frontmatter` dependency, keeping the
public-mirror gate's lock-freshness check green.
