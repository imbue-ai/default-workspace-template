The agent-facing `layout.py` helper now recognizes service panels by their
unguessable per-service origin label (`<name>-<rand>`). Stored panel URLs carry
this random label as their leading hostname component, so `layout.py` maps it
back to the service name via the app registry (`data/.state/apps.toml`) when
matching `service:` refs against the live layout.
