# Release channels publish per platform; CI verifies the Linux artifacts

`scripts/release_channel/publish.py` and `manifest.py` publish one `<channel>-<platform>.yml` per platform an entry lists in its new required `platforms` field, reading ToDesktop's per-platform build manifest for each. Listing `linux` for a build that ToDesktop never packaged for Linux refuses the whole entry, so the mac half is not published either, and the "still serving" report is per platform. Linux manifests keep both the `.AppImage` and `.deb` references, which the two Linux updaters select on.

`.github/workflows/minds-launch-to-msg.yml` gains a `linux_artifacts` job on an Ubuntu runner: it downloads the build's AppImage and `.deb`, extracts them without installing, and runs the new `apps/minds/scripts/verify-linux-artifacts.sh`, which checks that the Electron binary and every bundled tool is an x86-64 ELF that runs, that the `.deb` carries `resources/package-type`, and that the bundled wheels resolve into a working `minds` with the bundled `uv`. `update_marker` and the Slack summary treat it like the existing jobs.

The spec is `specs/minds-linux-packaging/spec.md`; `specs/minds-release-channels/spec.md` records Linux as built.
