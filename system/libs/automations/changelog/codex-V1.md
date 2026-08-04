`run_automation.sh` now stacks a harness template ahead of the role template when creating an
automation agent, matching mngr's harness/role template split. It defaults to `claude`, so
existing callers (including the Caretaker's scheduled check) are unaffected. `--transfer none`
is no longer passed on the command line: the `automation` and `caretaker` role templates own
that setting now.
