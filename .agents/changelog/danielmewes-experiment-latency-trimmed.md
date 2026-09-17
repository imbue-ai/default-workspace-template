Speed up the `build-app` workflow when creating and iterating on initial app mocks:

- Adds `--start` to `scaffold_flask_lib.py` to register the new app with supervisord and wait for `/health` in one step, removing the need for separate supervisorctl commands.
- Enables Werkzeug's reloader (`use_reloader=True`) in scaffolded Flask runner templates, enabling rapid mock iteration (~50ms reload) without restarting services.
- Adds `references/frontend-choices.md` with baseline design defaults (typography, cards, dark mode, copywriting) and removes the requirement in `build-app` and `update-app` to invoke external frontend design skills before writing markup.
- Updates verification guidance in `build-app` and `references/verify.md` to recommend `smoketest_app.py` for sub-second HTTP readiness and content verification.
