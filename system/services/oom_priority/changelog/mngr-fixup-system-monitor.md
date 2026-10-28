Added the `resource_sampler` program (`bin/resource_sampler.py`, policy in `resource_sampler.py`): every 5 s it reads each process's `/proc/<pid>/stat` once and records overall CPU (from the change in the sum of own plus reaped-children time, the only accounting that exists under gVisor) and memory under `data/.state/system-monitor/` (the last 10 minutes, a day of minute averages, and `cpu_stretch` events crediting a stretch of CPU above 90% to the registered agents and supervised programs that caused it) for the System Monitor app. It has its own service band, one past the app's, so it is the first built-in service shed.

The memory reader the app used (`meminfo.read_meminfo`) and the supervisord control-socket reader (`supervisor_rpc.supervised_pids`) moved into this package so the sampler and the app share them.

`supervisor_rpc` gained `stop_program`, which asks supervisord over its socket to stop one program (the System Monitor app's Quit for a user app); a program that is not running counts as stopped. The README's resource sampler section names the module and both of its functions.

`oom_priority.testing` gained `fake_supervisord`, a stand-in for supervisord's control socket that this package's tests and the System Monitor app's share.
