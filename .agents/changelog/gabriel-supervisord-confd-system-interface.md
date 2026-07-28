`system_interface` moves into `system/supervisord.conf.d/system_interface.conf` alongside every other service. `system/supervisord.conf` now declares no programs at all -- it is purely the supervisord daemon's own configuration plus the `[include]` of the drop-ins.

It was the one program left behind by the conf.d split, and only for an external reason: the minds desktop client's recovery probe read that file directly with `configparser`, which does not follow supervisord's `[include]`, so moving it would have silently broken the probe's port-listening and curl checks. The probe now asks supervisord for the port over its `getAllConfigInfo` RPC and falls back to an include-aware file read, so that constraint is gone.

Startup is unchanged: supervisord orders programs by `(priority, name)` and every program uses the default priority, so the declaring file has no effect on start order.

For this project: the service-process reference no longer carves out `system_interface` as an exception -- every program lives in a drop-in.
