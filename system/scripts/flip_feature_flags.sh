#!/usr/bin/env bash
# Flags: FEATURE_FLAG_ENABLE_OTHER_HARNESSES
set -euo pipefail

CONF=/home/user/workspace/system/supervisord.conf
BLOCK='/^\[program:system_interface\]/'

on='' off=''
for a in "$@"; do
  case $a in
    --on=*) on=${a#*=} ;;
    --off=*) off=${a#*=} ;;
    *) echo "usage: ${0##*/} [--on=FLAG,FLAG] [--off=FLAG,FLAG]" >&2; exit 2 ;;
  esac
done
[[ -n $on$off ]] || { echo "usage: ${0##*/} [--on=FLAG,FLAG] [--off=FLAG,FLAG]" >&2; exit 2; }

split() { tr ',' '\n' <<<"${1// /}" | grep . || true; }

old=$(sed -n "$BLOCK,/^\[/{s/^environment=//p}" "$CONF")
keep=$(split "$(sed 's/=[^,]*//g' <<<"$old")"; split "$on")
[[ -n $off ]] && keep=$(grep -vxFf <(split "$off") <<<"$keep" || true)
new=$(sort -u <<<"$keep" | grep . | sed 's/$/="1"/' | paste -sd, || true)

[[ $new == "$old" ]] && { echo "Nothing to flip; config already matches."; exit 0; }

sed -i "$BLOCK,/^\[/{/^environment=/d}" "$CONF"
[[ -n $new ]] && sed -i "$BLOCK a environment=$new" "$CONF"
echo "environment=${new:-<removed>}"

supervisorctl reread >/dev/null
supervisorctl update >/dev/null
supervisorctl restart system_interface

# The restart leaves every open view stale; this reloads them.
python3 "$(dirname "$0")/refresh_workspace_view.py"
