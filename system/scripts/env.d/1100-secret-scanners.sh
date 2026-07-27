#!/usr/bin/env bash
# env.d unit: the publish-inspiration scan gate's secret-scanner binaries
# (betterleaks + kingfisher). Normally baked into the image by setup_system.sh;
# this unit re-converges them if they ever go missing (or after an image that
# skipped them), reusing the single source of truth for version pins and
# per-arch sha256s.
#
# env.d contract: idempotent with a fast satisfied-check -- no markers.
set -euo pipefail

if command -v betterleaks >/dev/null 2>&1 && command -v kingfisher >/dev/null 2>&1; then
    echo "[env.d/secret-scanners] both scanners present, satisfied"
    exit 0
fi

REPO_ROOT="${ENV_CONVERGE_WORKSPACE_DIR:-/home/user/workspace}"
bash "$REPO_ROOT/scripts/install_secret_scanners.sh"
