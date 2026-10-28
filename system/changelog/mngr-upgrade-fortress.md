Upgrade Fortress (the Screen's stealth Chromium) from v151.0.7908.0 to v156.0.8078.47 in the Screen's env.d unit (`system/scripts/env.d/2000-screen.sh`), resolving imbue-ai/default-workspace-template#896.

- Both architectures now install from the official tiliondev/fortress release (the minhtrinh-imbue fork build that supplied arm64 at v151 is retired), checked against the pinned SHA-256 sums; the tarballs unpack to `fortress-v156/` and the stable symlink `/opt/fortress/tilion-fortress/tilion` is repointed as before.

- The CDP click hang that blocked the v153 upgrade is fixed upstream and verified with the issue's repro against the v156 linux-x64 binary: a Playwright click that moves the pointer returns (in up to about a second, the motion animation's cost) instead of hanging, over a direct launch and over `connect_over_cdp` alike, including on a page that re-renders on click.

- No license key is provisioned: unlicensed, Fortress prints one notice and runs the public first-generation engine, matching the fleet's v151 behavior (and this repository is public, so a shipped key would be too).

- The Chrome extensions are fetched for Chrome 156, and the pre-2026-10-18 legacy-layout satisfied check (a CLEANUP for images that unpacked v151 straight into the stable path) is dropped -- those images exist only on this branch's history and a layout that old now simply reinstalls.
