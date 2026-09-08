The root `uv.lock` now records `pyyaml==6.0.3` and `cachetools==7.1.4` as direct
entries of the remote-service-connector's `image` dependency group, and `pyyaml`
as a dependency of the package itself, which imports it.

`pyyaml` was already installed in that container as a transitive dependency, so
only `cachetools` is new to the image. The connector reads electron-updater's
channel manifests, which are YAML, and caches the resolved download link with
`cachetools.cached` over a `TTLCache` in place of a hand-rolled dict and lock.
Both are pure Python with no dependencies of their own.
