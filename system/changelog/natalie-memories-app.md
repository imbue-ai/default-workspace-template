Registered the new memories app's frontend in the npm workspace (`system/package.json`, its lockfile entries) and ignored its build output.

The image build copies the memories app's `pyproject.toml` and frontend `package.json` with the other members', so a freshly built workspace installs it, and the built-in manifest checks cover it.
