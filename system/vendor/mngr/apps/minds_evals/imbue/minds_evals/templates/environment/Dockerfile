# Minds box: a full Minds computer for an exact mngr SHA, adapted from the old
# apps/mngr_minds_eval box image. One image per mngr SHA; harbor's Modal
# provider builds it on Modal's builders and layer-caches it, so every task in
# a dataset (byte-identical environment/) reuses the same build.
#
# Differences from the old box image:
#   - no eval-app overlay (the driver is host-side; no harness code runs in-box)
#   - no ENTRYPOINT: harbor supplies its own keepalive, and the driver's
#     setup() starts entrypoint.sh explicitly with the per-trial env
FROM node:24.15.0-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

# Base tooling + the desktop stack: Xvfb (virtual display), openbox (window
# manager), x11vnc + noVNC/websockify (debug access via `modal shell`), dbus
# (Electron needs a session bus), and the Chromium/Electron runtime libraries.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates build-essential curl rsync openssh-client \
      xvfb openbox x11vnc novnc websockify dbus dbus-x11 xterm fonts-liberation \
      libgtk-3-0 libnss3 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libgbm1 \
      libxss1 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxkbcommon0 libatspi2.0-0 \
 && rm -rf /var/lib/apt/lists/* \
 && corepack enable

# The mngr source at the EXACT ref is staged into the build context by the
# generator (a fresh shallow clone of the remote tip, never an on-device
# checkout, .git stripped -- Modal's context upload drops it regardless).
# Copied in here, so the build performs no clone and needs no GitHub
# credentials; a different ref changes this layer's cache key. The exact SHA
# travels as a plain file for the driver to read.
COPY mngr /work/mngr
COPY mngr_sha /work/mngr_sha

WORKDIR /work/mngr
RUN uv sync --all-packages

# node deps (electron binary via the allowlisted postinstall, latchkey) +
# compiled CSS; pin pnpm from the branch's manifest. ensure-binaries downloads
# the bundled binaries (restic/git/lima/uv) that the app's dev mode expects.
RUN cd apps/minds \
 && PNPM_VER="$(node -p "let p=require('./package.json'); (p.packageManager&&p.packageManager.split('@')[1])||(p.engines&&p.engines.pnpm)||'10.33.4'")" \
 && corepack prepare "pnpm@${PNPM_VER}" --activate \
 && pnpm install \
 && (pnpm run build:css || echo "build:css failed (non-fatal)") \
 && (node scripts/ensure-binaries.js || echo "ensure-binaries failed (non-fatal)")

# Chromium for the UI-flow executor, which drives the delivered app's forwarded origin from inside
# the box. The playwright PYTHON package is already in the synced venv (apps/minds depends on it),
# so only the browser binary is missing, and the browser this installs is the revision that
# version pins. Both halves of the executor -- this install and the step script that drives it --
# run against that same venv, so they cannot drift apart: nothing outside the box has a say in
# which playwright either of them sees.
#
# Placed last on purpose: it invalidates nothing above it, so the expensive uv/pnpm layers stay
# cached when this line changes. Roughly +300 MB.
#
# `python -m playwright`, never the `playwright` console script: uv writes an absolute venv path
# into console-script shebangs, which breaks as soon as the workspace moves (see
# libs/mngr_imbue_cloud's changelog). PLAYWRIGHT_BROWSERS_PATH is absolute for the same class of
# reason -- the default lives under $HOME, and the box runs as root with $HOME not guaranteed.
# --with-deps is belt-and-braces: every library Chromium needs is already installed above.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN uv run python -m playwright install --with-deps chromium

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY probe_minds_port.py /usr/local/bin/probe_minds_port.py
RUN chmod +x /usr/local/bin/entrypoint.sh
