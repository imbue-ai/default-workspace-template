from imbue.imbue_common.conftest_hooks import register_conftest_hooks

register_conftest_hooks(globals())

# Generated harbor datasets and local job results live under this app but embed
# a full mngr-internal clone (plus arbitrary trial artifacts); pytest must
# never collect from them.
collect_ignore = ["datasets", "jobs"]
