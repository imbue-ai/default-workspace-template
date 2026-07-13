- The deferred-install service (`scripts/deferred_install.sh`, documented in
  this README) is now the BACKSTOP delivery path for the three secret-scanner
  binaries the publish-inspiration skill hard-requires: `betterleaks` v1.6.1
  (MIT, replacing gitleaks), `trufflehog` v3.95.9 (AGPL-3.0), and
  `kingfisher` v1.106.0 (Apache-2.0). The pins and hard-coded per-arch
  (x86_64 / aarch64) sha256 checksums live in the new shared
  `scripts/install_secret_scanners.sh`, which the Dockerfile runs at
  image-build time (primary delivery: scanners exist from the first second
  of every docker-built container) and which the deferred-install wrappers
  re-invoke per tool for providers not built from the Dockerfile (e.g.
  Lima). The shared script skips any tool already present at its pinned
  version without network access, so the deferred-install calls are instant
  no-ops on docker containers and only write the per-tool markers.

- Wrapper semantics are unchanged from the old gitleaks installer: install
  to `/usr/local/bin`, per-tool `done.<tool>` markers written only on
  success (a failed install retries next boot), and failures isolated from
  each other and from the playwright install. Unit tests moved with the
  logic: `install_secret_scanners_test.py` covers the shared installer
  (arch mapping, checksum accept/reject, skip-at-pin, per-tool isolation)
  and `deferred_install_test.py` covers the wrapper's marker semantics, with
  shared bash-test helpers in `bootstrap/testing.py`.
