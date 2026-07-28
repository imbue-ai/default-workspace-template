`system_interface` moves into `system/supervisord.conf.d/system_interface.conf` alongside every other service. `system/supervisord.conf` now declares no programs at all -- it is purely the supervisord daemon's own configuration plus the `[include]` of the drop-ins.

It was the one program left behind by the conf.d split, and only for an external reason: the minds desktop client's recovery probe read that file directly with `configparser`, which does not follow supervisord's `[include]`, so moving it would have silently broken the probe's port-listening and curl checks. The probe now asks supervisord for the port over its `getAllConfigInfo` RPC and falls back to an include-aware file read, so that constraint is gone.

Startup is unchanged: supervisord orders programs by `(priority, name)` and every program uses the default priority, so the declaring file has no effect on start order.

For this project: the README's description of where services are declared follows suit. The bootstrap is unchanged; it still execs `supervisord -n -c system/supervisord.conf`, and supervisord resolves the include from there.
