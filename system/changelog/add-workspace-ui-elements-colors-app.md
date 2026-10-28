The Style Guide app's frontend joins the npm workspace (`system/package.json`, its lockfile, and the Dockerfile's manifest copies), its built bundle is ignored like the other frontends', and its program runs on port 8060 (`system/supervisord.conf.d/style-guide.conf`).

The Classic Mac and Windows 2000 themes ship the Style Guide's icon (`icons/style-guide.png`): a painter's palette, one bit deep for Classic Mac, in shaded wood with four paints for Windows 2000.

`forward_port.py` copies a manifest's `desktop_shortcut` onto the app's registry row (and clears it when a manifest drops it), and the desktop interface's contracts and plan document the field.

`system/supervisord.conf.d/style-guide.conf` no longer autostarts; `style_guide_register.conf` registers the app at boot and exits.
