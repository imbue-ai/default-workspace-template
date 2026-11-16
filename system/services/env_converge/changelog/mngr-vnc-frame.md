The `UnitRunResult` example in `data_types.py` names `2000-screen.sh`, the env.d unit that replaced `1000-playwright-fortress.sh` when the Screen's stack took over the browser's install.

The upgrade's apt steps pass `-oDPkg::Lock::Timeout=600`, so an `env-converge upgrade` that runs while the boot-time env-converge is still installing the Screen's stack waits for the dpkg lock instead of failing at once with "Could not get lock /var/lib/dpkg/lock-frontend".
