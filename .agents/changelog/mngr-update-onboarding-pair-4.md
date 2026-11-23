The `build-app` skill's scaffolder sets the workspace's `app_build_started` onboarding marker on every run, so the chat does not suggest building an app while one is being built. A run that cannot write the marker prints a warning and scaffolds as before.

The `first-run-onboarding` skill lists Books, Movies and Music as three Creative categories in place of "Books, movies & music", and its ideas reference has a row of ideas for each. The skill also says that the pickers' "Other" pills put "Write whatever you want!" and "Write whatever data you want here!" in the message as prompts to write over, so either sentence arriving unchanged names no category and no app.

The `update-self` skill counts the Onboarding checklist's frontend among the bundles its apply builds, installs, snapshots and verifies (`--worker-bundle onboarding_checklist=<path>`), so a worker now reports eight built bundles.
