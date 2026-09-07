- Desktop Lima VMs now download their pinned Debian 13 (trixie) guest image from
  imbue's artifact mirror (`https://apt.imbuepackages.com/artifacts/debian-cloud-image/...`)
  instead of `cloud.debian.org`, which prunes old releases and would eventually
  break every Lima create at the pinned snapshot. Same image bytes, same release
  as the gen-2 cloud slices; mngr's `minds-admin artifacts upload` publishes a new
  release to the mirror before the pin here is bumped.
