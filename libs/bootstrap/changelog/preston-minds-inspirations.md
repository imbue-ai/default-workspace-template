- The three secret-scanner binaries the publish-inspiration skill hard-requires
  -- `betterleaks` v1.6.1 (MIT, replacing gitleaks), `trufflehog` v3.95.9
  (AGPL-3.0), and `kingfisher` v1.106.0 (Apache-2.0) -- are installed by a new
  shared `scripts/install_secret_scanners.sh`, the single source of truth for
  the version pins and hard-coded per-arch (x86_64 / aarch64) sha256 checksums.
  The Dockerfile runs it at image-build time, so the scanners exist from the
  first second of every container built from the workspace image. If a binary
  is ever missing, the script is runnable by hand to install all three (it
  skips any tool already present at its pinned version without network access,
  so a redundant run is an instant no-op); the scan gate names that command in
  its missing-scanner error. The deferred-install service does NOT deliver the
  scanners -- it stays limited to heavy non-boot packages (Chromium/Playwright).

- `install_secret_scanners_test.py` covers the shared installer (arch mapping,
  checksum accept/reject, skip-at-pin, per-tool isolation), with shared
  bash-test helpers in `bootstrap/testing.py`.
