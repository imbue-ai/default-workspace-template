# Retry transient download failures in setup_system.sh

All pinned-binary downloads in `system/scripts/setup_system.sh` (ttyd, restic, gh, caddy, frpc, the uv and Claude Code installers) now run curl with `--retry 5 --retry-all-errors --retry-delay 5`. Previously a single transient failure -- e.g. a GitHub releases 503, which aborted a minds e2e snapshot image build in CI -- killed the whole image build or VM provision under `set -e`. `install_secret_scanners.sh` already retried; this brings the rest of the setup downloads in line.
