# Removing an app

1. `python3 system/scripts/forward_port.py --name <name> --remove` (drops the
   entry from `data/.state/apps.toml`).
2. Stop the program and delete its drop-in, then reconcile:

   ```bash
   supervisorctl stop <name>
   rm system/supervisord.conf.d/<name>.conf
   supervisorctl reread && supervisorctl update
   ```

   (See `.agents/shared/references/service-processes.md` for the
   mechanics.)
3. If you scaffolded a lib, also: `rm -rf system/apps/<package>/`. The root
   `pyproject.toml` needs no edit -- the scaffold never added one.
