# The fleet's Chromium engine: CloakBrowser

The browser fleet drives [CloakHQ/CloakBrowser](https://github.com/CloakHQ/CloakBrowser)
(a from-source C++/Blink/V8 stealth-patched Chromium fork), pulled from a pinned
GitHub release under their free "delayed release" tier (chosen over
`tiliondev/fortress` specifically because Fortress ships no Linux arm64 build,
which would break the desktop/Lima path on Apple Silicon; CloakBrowser does).
The binary lands at `/opt/cloakbrowser/chrome`, fetched and SHA256-verified by
`scripts/deferred_install.sh`'s `_install_cloakbrowser` on first container
boot (and pre-baked into the box image ahead of time on cloud slices, see
`slice_provider.py::_build_cloakbrowser_derived_image` in `mngr`). The only
code that changed to point at it: `_CLOAKBROWSER_MARKER` /
`_CLOAKBROWSER_EXECUTABLE` in `libs/browser/src/browser/session.py` (replacing
`_PLAYWRIGHT_MARKER` and `playwright.chromium.executable_path`), and the
matching `_CLOAKBROWSER_*` constants in `scripts/deferred_install.sh`.

## Bumping the CloakBrowser version

1. Pick a release tag from <https://github.com/CloakHQ/CloakBrowser/releases>
   with real (non-Pro-gated) `cloakbrowser-linux-x64.tar.gz` /
   `-linux-arm64.tar.gz` assets -- check the release actually has downloadable
   binaries, not just `SHA256SUMS` (their latest major is routinely paywalled;
   the newest release *with* public binaries is the one to use).
2. Update `_CLOAKBROWSER_VERSION` and the two `_CLOAKBROWSER_SHA256_*`
   constants in `scripts/deferred_install.sh` from that release's
   `SHA256SUMS` file.
3. Update the matching `_CLOAKBROWSER_VERSION` / `_CLOAKBROWSER_SHA256_X64` in
   `mngr`'s `slice_provider.py` (cloud is x64-only, no arm64 constant there) --
   these are two independently-pinned copies, kept in sync by hand, not code.

Nothing else needs to change -- the binary always unpacks to the same
`/opt/cloakbrowser/chrome` path regardless of version.

## Swapping to a different engine entirely (e.g. Fortress, once/if it ships arm64)

Same two touch points, nothing architectural:

1. In `scripts/deferred_install.sh`'s `_install_cloakbrowser` (rename it),
   replace the release URL, asset name(s), and SHA256 pin(s) with the new
   engine's. **Download and inspect the actual tarball first** to find the
   real binary path inside it -- don't assume a name or directory structure.
   (CloakBrowser's turned out to be a flat archive with the binary literally
   named `chrome`; Fortress's quick-start docs implied a `tilion-fortress/`
   wrapper dir with a `tilion` binary inside -- these differ per project.)
2. Update `_CLOAKBROWSER_EXECUTABLE` in `session.py` to that real path.
3. Mirror both changes in `mngr`'s `slice_provider.py`
   (`_build_cloakbrowser_derived_image`) for the cloud pre-bake.

Any Chromium-family fork that accepts standard launch flags (`--headless`,
`--user-data-dir`, `--remote-debugging-port`, `--no-sandbox`) and speaks CDP
satisfies the whole contract these two files rely on -- `browser_use` and the
CDP observer don't know or care which engine is behind `executable_path`.
