The mirror now also serves the slice fleet's pinned non-apt artifacts and Docker's apt repo (imbue-ai/mngr-internal#851):

- New `GET /artifacts/<name>/<version>/<subpath>` Worker route serving pinned artifacts (cloud images, gVisor, age, s5cmd, uv, onetun, the otel collector) straight from R2 with immutable caching and Range support. No read-through: a missing artifact is a 404 until `minds-admin artifacts upload` stores it. HEAD requests on the pool and artifact routes now use a metadata-only lookup.

- New `docker` archive (`https://download.docker.com/linux/debian`, suite `trixie`, component `stable`). Docker has no snapshot service and keeps its package files under `dists/<suite>/pool/`, so a cut freezes its live indexes (a re-cut at the same timestamp keeps what the first run froze, reading the manifest back from the stored InRelease) and `warm` freezes the listed `.deb`s under the same `snap/<T>/docker/...` prefix, where the dists route serves them. Archives are now described by `ArchiveSource` (`DEFAULT_ARCHIVES` in `data_types.py`) with a per-archive index source and component list.

- Package lists accept apt-style `name=version` pins; a bare name now resolves to the newest version in the frozen index (dpkg ordering, via `python-debian`) rather than every version. New `package_lists/docker.txt` pins the engine + containerd to the versions `libs/mngr_vps` installs and takes the newest buildx/compose plugins and `docker-ce-rootless-extras` (a default-installed Recommends of the engine).

- Resolution reads `Packages.xz`, `.gz`, or plain (Docker publishes no `.xz`); `resolve_package_names` is now `resolve_package_specs`, and warm/verify results report `missing_paths` / `unresolved_specs`.

- README: the archives section, the artifacts section (upload flow, upload-before-bump rule), and the DNS-sinkhole runbook for proving a prep reaches no upstream host.
