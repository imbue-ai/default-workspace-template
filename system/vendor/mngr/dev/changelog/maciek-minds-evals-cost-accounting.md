The `test-minds-evals` CI job's path gate lists the monorepo packages that land in that app's venv,
`libs/mngr` among them. `libs/mngr_usage` is not one of them, so a change confined to it does not run
the minds-evals suite.
