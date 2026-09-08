from imbue.imbue_common.conftest_hooks import register_conftest_hooks

register_conftest_hooks(globals())

# Generated harbor datasets and local job results live under this app but embed
# a full mngr-internal clone (plus arbitrary trial artifacts); pytest must
# never collect from them.
collect_ignore = ["datasets", "jobs"]

# The ROOT pytest run loads the conftests under this app too: `testpaths = ["apps/*"]` hands pytest
# this directory as an explicit argument, and the root conftest's `collect_ignore_glob` stops it
# from collecting the files below but not from descending the directories, so every conftest it
# meets on the way is imported. The root venv has no harbor, and nearly every `imbue.minds_evals`
# module imports it, so a conftest here may import from this package only through a stdlib-only
# module such as `template_loading.py`; anything else aborts every root-level run at collection.
