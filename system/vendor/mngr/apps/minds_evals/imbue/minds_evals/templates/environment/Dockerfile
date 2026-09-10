# Minds box: a full Minds computer for an exact mngr SHA. harbor's Modal
# provider hands this whole file to `modal.Image.from_dockerfile`, which
# compiles it into ONE image build with no per-instruction cache: the cache key
# is every command here plus the build context, so there is one image per mngr
# SHA (about two and a half minutes to build cold, seconds on a hit), reused by
# every task in a dataset because their environment/ trees are byte-identical.
# Any change to the staged mngr clone rebuilds all of it -- reordering the
# instructions buys nothing, and moving the third-party steps out of the
# per-SHA build is the only thing that would.
#
# The image carries no eval application: the conversation driver runs host-side
# and reaches into the box over the system interface, so the only harness code
# baked in is the entrypoint and the port probe the driver invokes. There is
# deliberately no ENTRYPOINT either -- harbor supplies its own keepalive, and
# the driver's setup() starts entrypoint.sh explicitly with the per-trial env.
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
# credentials; a different ref changes the image's cache key. The exact SHA
# travels as a plain file for the driver to read.
COPY mngr /work/mngr
COPY mngr_sha /work/mngr_sha

WORKDIR /work/mngr
RUN uv sync --all-packages

# node deps (electron binary via the allowlisted postinstall, latchkey), with
# pnpm pinned from the checked-out manifest. ensure-binaries downloads the
# bundled binaries (restic/git/lima/uv) that the app's dev mode expects.
RUN cd apps/minds \
 && PNPM_VER="$(node -p "let p=require('./package.json'); (p.packageManager&&p.packageManager.split('@')[1])||(p.engines&&p.engines.pnpm)||'10.33.4'")" \
 && corepack prepare "pnpm@${PNPM_VER}" --activate \
 && pnpm install \
 && (node scripts/ensure-binaries.js || echo "ensure-binaries failed (non-fatal)")

# Chromium for the UI-flow executor, which drives the delivered app's forwarded origin from inside
# the box. The playwright PYTHON package is already in the synced venv (apps/minds depends on it),
# so only the browser binary is missing, and the browser this installs is the revision that
# version pins. Both halves of the executor -- this install and the step script that drives it --
# run against that same venv, so they cannot drift apart: nothing outside the box has a say in
# which playwright either of them sees.
#
# Roughly +300 MB, and about 25-30 s of every cold build.
#
# `python -m playwright`, never the `playwright` console script: uv writes an absolute venv path
# into console-script shebangs, which breaks as soon as the workspace moves (see
# libs/mngr_imbue_cloud's changelog). PLAYWRIGHT_BROWSERS_PATH is absolute for the same class of
# reason -- the default lives under $HOME, and the box runs as root with $HOME not guaranteed.
# --with-deps is belt-and-braces: every library Chromium needs is already installed above.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN uv run python -m playwright install --with-deps chromium

# The in-box LLM proxy (`--ak proxy`) is served from a venv of its own, because the workspace venv
# above cannot serve it: modal-litellm depends on plain `litellm` there (the [proxy] extra's
# gunicorn cap is confined to that app's image group), so its `litellm` CLI exits with a missing
# dependency, and every `uv run` in the box re-syncs that venv from the lock, which would strip
# proxy dependencies out from under a running proxy. The pin set is modal_litellm's hash-locked
# image export as of the box's mngr SHA, so a trial's proxy runs the versions the deployed proxy of
# that revision runs, on the python:3.12 its image installs those pins against. The versions only:
# that image also generates a prisma client on top of them, which a box's proxy, serving without a
# database, never reaches.
RUN uv venv --python 3.12 /opt/eval_proxy \
 && uv pip install --python /opt/eval_proxy --require-hashes -r apps/modal_litellm/image_requirements.txt

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY probe_minds_port.py /usr/local/bin/probe_minds_port.py
RUN chmod +x /usr/local/bin/entrypoint.sh
