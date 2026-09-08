- The `minds-evals-view` recipe serves a harbor job directory with the viewer frontend vendored into
  `apps/minds_evals/viewer/`, building it on demand with a project-local bun. Bun stays confined to
  the minds evals recipes; no other recipe needs it.

- CI builds the vendored viewer as part of the existing `test-minds-evals` job. It is source rather
  than a committed bundle, so nothing else would notice if it stopped building.

- The generated `.dockerignore` re-includes the vendored viewer's `app/lib/`. Only the repo-root
  `.gitignore` feeds that file, so a path re-included by a nested `.gitignore` needs a docker-form
  negation appended or it silently vanishes from offload sandbox images.
