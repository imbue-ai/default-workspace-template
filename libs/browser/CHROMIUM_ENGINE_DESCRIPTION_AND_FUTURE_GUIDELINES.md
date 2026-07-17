# Chromium engine: description and future guidelines

## What this is, in three sentences

The browser fleet drives [CloakHQ/CloakBrowser](https://github.com/CloakHQ/CloakBrowser)
(a from-source C++/Blink/V8 stealth-patched Chromium fork), pulled from a
pinned GitHub release under their free "delayed release" tier -- chosen over
`tiliondev/fortress` specifically because Fortress ships no Linux arm64 build
(would break the desktop/Lima path on Apple Silicon), while CloakBrowser does.
The binary lands at `/opt/cloakbrowser/chrome`, fetched and SHA256-verified by
`scripts/deferred_install.sh`'s `_install_cloakbrowser` on first container
boot, and pre-baked into the box image ahead of time on cloud slices (see
`slice_provider.py::_build_cloakbrowser_derived_image` in `mngr`). Exactly
which named things changed to point at it -- and, just as important, which
same-named things belong to a *different* library and did **not** change --
is spelled out explicitly below; don't guess from the diff.

## Exactly which variables changed -- and whose they are

Two different libraries are involved here, Playwright and browser-use, and
they are **not the same thing** even though one property and one keyword
argument happen to share the identical name `executable_path`. Mixing them up
is the easiest way to misread this code. Table, in the order they appear in
`libs/browser/src/browser/session.py`:

| # | Name | File | Whose namespace | What happened |
|---|---|---|---|---|
| 1 | `_PLAYWRIGHT_MARKER` → `_CLOAKBROWSER_MARKER` | `session.py` | **Ours** -- a plain module-level constant we invented, not part of any library's API | Renamed. A `pathlib.Path` gating `deferred_install_ready()`. Value changed from `Path("/var/lib/minds/deferred-install/done.playwright")` to `Path("/var/lib/minds/deferred-install/done.cloakbrowser")`. |
| 2 | `playwright.chromium.executable_path` | `session.py` | **Playwright's own property**, on Playwright's `BrowserType` object (`playwright.chromium`) | **Deleted, no longer read at all.** This is Playwright's own API -- a read-only attribute that returns wherever Playwright's *own* browser-management system downloaded *its own* managed Chromium binary. It has nothing to do with browser-use. We used to read this to find a Chromium to launch; we don't anymore. |
| 3 | `chromium_path` | `session.py` | **Ours** -- an ordinary local Python variable inside `LiveBrowser.start()`, belongs to neither library | Still exists, but its source changed: it used to be assigned from reading #2 (Playwright's property); it's now assigned from #4 (our own constant). |
| 4 | `_CLOAKBROWSER_EXECUTABLE` | `session.py` | **Ours** -- new module-level constant | New. A plain string, `"/opt/cloakbrowser/chrome"`. This is what #3 is now set to. |
| 5 | `executable_path=` | `session.py` | **browser-use's own keyword argument**, on the `browser_use.BrowserSession(...)` constructor call inside `_build_bu_session` | **Still used, value changed.** This is browser-use's own API -- a constructor parameter that tells browser-use which literal binary file to launch and drive. This is the one that actually matters for what engine runs. It used to receive whatever #2 (Playwright's property) returned; it now receives #3/#4 (our CloakBrowser path). Despite sharing the string `"executable_path"` with #2, #2 and #5 are unrelated attributes on two different objects from two different libraries -- #2 is Playwright reporting its own state, #5 is browser-use accepting a value *we* hand it. |
| 6 | `_CLOAKBROWSER_VERSION`, `_CLOAKBROWSER_INSTALL_DIR`, `_CLOAKBROWSER_RELEASE_URL`, `_CLOAKBROWSER_SHA256_ARM64`, `_CLOAKBROWSER_SHA256_X64` | `scripts/deferred_install.sh` | **Ours** -- bash `readonly` variables | New. Control what gets fetched, verified, and where it's unpacked. |
| 7 | `_CLOAKBROWSER_VERSION`, `_CLOAKBROWSER_SHA256_X64`, `_CLOAKBROWSER_INSTALL_DIR`, `_CLOAKBROWSER_RELEASE_URL` | `mngr`'s `slice_provider.py` | **Ours** -- Python `Final[str]` constants | New. Independently-pinned mirror of #6 for the cloud box pre-bake (x64 only, no arm64 constant there -- cloud slices are x86_64 bare metal). Kept in sync with #6 **by hand**, not by shared code -- see the upgrade steps below. |

The `playwright` Python **package** itself is still a dependency and still
installed (`pyproject.toml`, unchanged) -- it's still used for the CDP
*observer* connection (`playwright.chromium.connect_over_cdp`, protocol-level,
engine-agnostic) and by any agent's own direct scripted use
(`from playwright.sync_api import sync_playwright`). Only its *browser*
download (row 2, and the `deferred_install.sh` step that used to fetch it) is
gone.

## Upgrading CloakBrowser to a new version

1. Browse <https://github.com/CloakHQ/CloakBrowser/releases> and pick a tag
   that actually has downloadable `cloakbrowser-linux-x64.tar.gz` /
   `-linux-arm64.tar.gz` assets attached -- not just `SHA256SUMS` /
   `SHA256SUMS.sig`. Their newest major version is routinely Pro-gated (no
   public binary at all); the release you want is the newest one *with* real
   assets, which may be one or more majors behind their latest tag.
2. Download that release's `SHA256SUMS` file and copy the `x64`/`arm64` hash
   lines out of it.
3. In `scripts/deferred_install.sh`, update the three constants together:
   `_CLOAKBROWSER_VERSION` (the release tag), `_CLOAKBROWSER_SHA256_X64`, and
   `_CLOAKBROWSER_SHA256_ARM64`.
4. In `mngr`'s `slice_provider.py`, update the matching
   `_CLOAKBROWSER_VERSION` and `_CLOAKBROWSER_SHA256_X64` (no arm64 constant
   there -- see row 7 above). These are a **separate, manually-duplicated
   pin** -- forgetting this step means cloud slices keep baking the old
   version while desktop/Lima gets the new one.
5. Nothing else needs to change. The binary always unpacks to the same
   `/opt/cloakbrowser/chrome` path regardless of version -- verify this stays
   true for the new release by actually downloading and `tar tzf`-listing it
   (don't assume; CloakBrowser ships a flat archive with the binary literally
   named `chrome`, but a future release could restructure that).
6. Re-run `libs/browser/browser_test.py` and, ideally, boot a real workspace
   off the change and drive the fleet once before merging.

## When Fortress ships Linux arm64 (or any other engine swap)

Fortress (`tiliondev/fortress`) was the first candidate considered here and
was rejected on exactly one blocking fact: no Linux arm64 build, native or
Docker. Their own roadmap lists `linux/arm64 Docker image` as an unshipped
item. **When that changes, it's worth actively re-evaluating**, not just
noting the option -- Fortress's stealth claims (0% CreepJS headless/stealth,
a published gauntlet, monthly Chromium rebase) are stronger than CloakBrowser's
free tier, which trails their paid tier by roughly one to two Chromium majors
at any given time. Check <https://github.com/tiliondev/fortress/releases> and
their roadmap section for arm64 status.

If/when a swap is worth doing, same two touch points as any engine swap, no
architecture change required:

1. **Download and inspect the actual release tarball first.** Do not assume
   the binary's name or directory layout from a README. (CloakBrowser turned
   out to be a flat archive with the binary literally named `chrome`;
   Fortress's own quick-start docs implied a `tilion-fortress/` wrapper
   directory with a `tilion` binary inside -- these are not the same shape,
   and guessing wrong here fails silently until the fleet tries to launch.)
2. In `scripts/deferred_install.sh`, replace the CloakBrowser-specific bits in
   (or rename) `_install_cloakbrowser`: the release URL, asset name(s), and
   SHA256 pin(s) -- rows 6 above.
3. In `session.py`, update `_CLOAKBROWSER_EXECUTABLE` (row 4) to the new
   binary's real path.
4. Mirror both of the above in `mngr`'s `slice_provider.py`
   (`_build_cloakbrowser_derived_image`) for the cloud pre-bake -- row 7.

That's the entire contract. Any Chromium-family fork that accepts standard
launch flags (`--headless`, `--user-data-dir`, `--remote-debugging-port`,
`--no-sandbox`) and speaks CDP satisfies it -- browser-use's
`BrowserSession(executable_path=...)` (row 5) and the CDP observer connection
don't know or care which engine is behind that path.
