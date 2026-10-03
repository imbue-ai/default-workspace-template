- The update-self apply installs the `mngr` tool's entry points into the bin directory
  that pairs with the tool directory it is refreshing, not into whatever directory the
  `mngr` it found on `PATH` happened to sit in. A workspace whose `PATH` reaches the
  build's `/usr/local/bin/mngr` symlink first is refreshed in place as before.
