The shell's `service_registered` events now carry `shareable`: `true` only when the app's row is shareable and not internal. The minds Share tab reads it to decide which apps to offer, so it no longer has to hardcode app names. A change to an app's shareability is re-announced like a change to its URL or label.

The no-window rule no longer lets a per-app share grant keep an internal or unshareable app running (such as Getting Started): the share gateway admits nobody through such a grant, so it stands in for no window.
