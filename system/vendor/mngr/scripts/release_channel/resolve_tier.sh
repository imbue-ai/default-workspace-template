#!/usr/bin/env bash
# Resolve one tier's update-feed coordinates from files already in the repo, so
# the workflow carries no copies that can drift from the tier's own config.
#
# Emits, to $GITHUB_OUTPUT:
#   app_id         the ToDesktop app id (from apps/minds/todesktop.js)
#   bucket         the R2 bucket name (derived, same rule setup_tier.py uses)
#   feed_base_url  update_feed_base_url from the tier's client.toml, or empty
#   lima_base_url  lima_image_base_url from the tier's client.toml, or empty
#
# Bare values, never ready-made flags: the caller builds its own argv, so a value
# with a space in it stays one argument instead of splitting into two.
#
# An empty feed_base_url means the tier serves no channel manifest yet, and the
# caller skips publishing. A missing client.toml is an error, so that state
# cannot be reached by accident. Usage: resolve_tier.sh <tier>
set -euo pipefail

TIER="${1:?usage: resolve_tier.sh <tier>}"
CLIENT_TOML="apps/minds/imbue/minds/config/envs/${TIER}/client.toml"

if [ ! -f "$CLIENT_TOML" ]; then
  echo "error: no client.toml for tier '${TIER}' at ${CLIENT_TOML}" >&2
  exit 1
fi

APP_ID="$(node -e "console.log(require('./apps/minds/todesktop.js').id)")"
# The tier arrives as an argument rather than spliced into the program text, so
# a name carrying a quote cannot end up as Python source.
BUCKET="$(uv run python -c "
import sys
from scripts.r2.setup_tier import UPDATE_FEED, bucket_name
print(bucket_name(sys.argv[1], UPDATE_FEED))
" "$TIER")"

read_key() {
  # The committed client.toml is flat `key = "value"`; an absent key is empty,
  # not an error. `grep -m1` rather than `| head -1` so pipefail cannot turn a
  # SIGPIPE'd grep into that same empty answer. `cut -s` so a value this cannot
  # read -- a single-quoted one, say -- is empty too, rather than the whole line
  # passed through as if it were a URL.
  grep -m1 -E "^${1}[[:space:]]*=" "$CLIENT_TOML" | cut -s -d'"' -f2 || echo ""
}

FEED_BASE_URL="$(read_key update_feed_base_url)"
LIMA_BASE_URL="$(read_key lima_image_base_url)"

{
  echo "app_id=${APP_ID}"
  echo "bucket=${BUCKET}"
  echo "feed_base_url=${FEED_BASE_URL}"
  echo "lima_base_url=${LIMA_BASE_URL}"
} >> "${GITHUB_OUTPUT:-/dev/stdout}"
