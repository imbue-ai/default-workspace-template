# Carry app icons in service events

`service_registered` events now carry the app's registered SVG icon markup
(verbatim from apps.toml) alongside the url and label, so the minds shell can
draw each app's own icon on its Share page. Consumers sanitize before
inlining; rows without an icon are unchanged.
