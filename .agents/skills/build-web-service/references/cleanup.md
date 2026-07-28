# Removing a web service

1. `python3 system/scripts/forward_port.py --name <name> --remove` (drops the
   entry from `data/.state/applications.toml`).
2. Stop the program and delete its file, then reconcile:

   ```bash
   supervisorctl stop <name>
   rm system/supervisord.conf.d/<name>.conf
   supervisorctl reread && supervisorctl update
   ```

   (See `.agents/shared/references/service-processes.md` for the
   mechanics.)
3. If you scaffolded a lib, also `rm -rf creations/<package>/`. Nothing in the
   root `pyproject.toml` needs reverting -- the scaffolder does not touch it
   (the `creations/*` member glob covers the package), so removing the
   directory and the drop-in is the whole teardown.
