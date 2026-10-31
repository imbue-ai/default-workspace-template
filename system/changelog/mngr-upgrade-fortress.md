Upgrade Fortress (the Screen's stealth Chromium) from v151.0.7908.0 to v156.0.8078.47 in the Screen's env.d unit (`system/scripts/env.d/2000-screen.sh`), resolving imbue-ai/default-workspace-template#896.

- Both architectures now install from the official tiliondev/fortress release (the minhtrinh-imbue fork build that supplied arm64 at v151 is retired), checked against the pinned SHA-256 sums; the tarballs unpack to `fortress-v156/` and the stable symlink `/opt/fortress/tilion-fortress/tilion` is repointed as before.

- The CDP click hang that blocked the v153 upgrade is fixed upstream and verified with the issue's repro against the v156 linux-x64 binary: a Playwright click that moves the pointer returns (in up to about a second, the motion animation's cost) instead of hanging, over a direct launch and over `connect_over_cdp` alike, including on a page that re-renders on click.

- No license key is provisioned: unlicensed, Fortress prints one notice and runs the public first-generation engine, matching the fleet's v151 behavior (and this repository is public, so a shipped key would be too).

- v156 replays a recorded human cursor trajectory for every pointer move it is sent (v151 teleported the pointer): a click on a target away from the pointer's last position returns in up to about a second, and after the press the cursor glides across whatever lies between the two points, so a hover-sensitive control can change under a test that reads it right after such a click. `AGENTS.md` says so beside the Playwright instructions, and the one workspace test this caught (the chat's auto-compact e2e test) drives its submenu with the keyboard.

- The Chrome extensions are fetched for Chrome 156, and the pre-2026-10-18 legacy-layout satisfied check (a CLEANUP for images that unpacked v151 straight into the stable path) is dropped -- those images exist only on this branch's history and a layout that old now simply reinstalls.

- The eMudhra CA distrust policy (the `CADistrustedCertificates` machine policy distrusting the eight compromised intermediates, shipped on main in the old `1000-playwright-fortress.sh`) is ported into `2000-screen.sh` with its test, since this branch deletes that unit and forked before the policy landed. v156 is still below the 158.0.8115.0 threshold at which Chromium's own root store and SPKI blocklist make the policy removable.
