#!/bin/bash
# Runs the command it is given with both of its output streams going through
# earlyoom-stamp, which timestamps each line, to this script's stderr. exec keeps
# the command at this script's pid, so supervisord signals and tracks it directly.
set -euo pipefail
stamp="$(dirname "${BASH_SOURCE[0]}")/earlyoom-stamp"
exec "$@" > >(exec "$stamp" >&2) 2>&1
