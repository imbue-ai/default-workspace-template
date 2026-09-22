Merges `mngr/linux-packaging` (see `mngr-linux-packaging.md`): `/download` learns the `linux-deb-x64` and `linux-appimage-x64` platforms (`linux` aliases the `.deb`), each read from `stable-linux.yml` by artifact extension, answering 404 with no download event recorded until stable publishes a Linux manifest.

A 4xx read of a stable channel manifest (the 404 for `stable-linux.yml` until stable lists linux) is no longer retried; only transient read failures are.
