# creations/

Things this mind has built: apps, dashboards, tools, and services. Each
creation lives in its own folder here.

- A creation's *code* lives in `creations/<name>/`.
- A creation's *stored data* lives in `data/creations/<name>/`, so the code
  stays clean and the data is covered by the workspace backup.

Creations are usually made with the `build-web-service` skill (which scaffolds
a small web service here) or adopted from a published *inspiration* -- a
shareable snapshot of another mind's creations. Publishing this mind's own
creations as an inspiration is what the `publish-inspiration` skill does.

Python packages in this folder are picked up automatically by the workspace's
`creations/*` uv member glob -- no central registration needed beyond the root
`pyproject.toml` dependency the scaffolder adds.
