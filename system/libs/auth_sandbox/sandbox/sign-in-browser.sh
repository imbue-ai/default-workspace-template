#!/bin/bash
# The browser latchkey launches for a sign-in (the template config.json names it): the Fortress launcher, with
# its window at the display's top left corner. Chromium places a new window 10 px in from the corner and
# Playwright then sizes it to its 1280x720 viewport, a 1288x805 window, which the Screen's display
# (SCREEN_FIXED_SIZE in boot.sh) holds only at the corner; latchkey passes Chromium no arguments of ours.
#
# Installed at /opt/auth-sandbox/sign-in-browser.sh by auth_sandbox.modal_image_build; its source is
# system/libs/auth_sandbox/sandbox/sign-in-browser.sh.
set -euo pipefail
exec /opt/fortress/tilion-fortress/tilion --window-position=0,0 "$@"
