Made the provisioning scripts resilient to transient network failures when fetching pinned binaries.

`system/scripts/setup_system.sh` now retries each `curl` download of the pinned CLIs (restic, ttyd, gh, caddy, frpc, uv, Claude Code) via a shared `--retry 5 --retry-all-errors --retry-delay 2` flag set, so a transient GitHub-release 503 or a mid-transfer connection drop (curl exit 56) no longer aborts the whole provision under `set -e` -- the same failure mode that was intermittently failing the minds snapshot image build.

`system/scripts/install_secret_scanners.sh` now uses the same retry flag set for its scanner-tarball download (previously `--retry 3` with no `--retry-all-errors`), so the two provisioning scripts retry identically.

Every download is still sha256-verified, so a retry cannot admit a corrupt artifact.
