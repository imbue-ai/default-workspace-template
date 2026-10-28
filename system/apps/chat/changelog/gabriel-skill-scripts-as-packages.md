The tests that pin `message_chat.py` and `run_in_background.py` constants import them from the `workspace_bare_scripts` package, now a chat dev dependency, instead of loading the files by path, and the one that pins update-self's resume note imports `update_self_skill.update_resume_note` (`update-self-skill` is a chat dev dependency too).

The README and the account index lock's docstring no longer mention `migrate_claude_auth.py`, which this release deletes.
