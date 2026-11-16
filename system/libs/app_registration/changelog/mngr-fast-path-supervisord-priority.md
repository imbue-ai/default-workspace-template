`forward_port.load_apps` is public, with a docstring naming what it raises, so `system/scripts/wait_for_critical_path.py` reads the app registry through the same loader the registry's writer uses.
