Added `docs/system/specs/system-monitor.md`, the design of the System Monitor app and its resource sampler, and the `resource_sampler` supervisord program in `system/supervisord.conf.d/`. The docs and the manifest contract tables follow the app's new name.

The spec gained the breakdown's accounting rules (anonymous memory, a System row that makes the memory and CPU columns add up), the Workspace row's items, the disk walk's items, and a section on the Stop buttons (with `POST /api/stop` in its route table); the disk view's figure is documented as the machine's whole disk.
