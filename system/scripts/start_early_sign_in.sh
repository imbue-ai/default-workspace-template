#!/usr/bin/env bash
# Start the early sign-in (system/apps/chat/imbue/chat/early_sign_in.py): the command the Imbue
# Studio app runs over SSH while a new workspace is built, before supervisord runs anything.
#
# An SSH session is not a supervisord program, so this starts the program the way the chat app's
# own program starts (system/supervisord.conf.d/chat.conf): from the repo root, with the workspace
# venv active as bootstrap activates it for supervisord, and with supervisord's environment
# (system/supervisord.conf). The sign-in CLIs then run in the environment they run in once the
# workspace is up. HOME is root's passwd home, which an SSH session already has, as supervisord does.
set -euo pipefail

cd "$(dirname "$0")/../.."
VIRTUAL_ENV="$(cd .venv && pwd -P)"
export VIRTUAL_ENV
export PATH="$VIRTUAL_ENV/bin:$PATH"
export MNGR_ALLOW_UNKNOWN_CONFIG=1
exec .venv/bin/chat-early-sign-in "$@"
