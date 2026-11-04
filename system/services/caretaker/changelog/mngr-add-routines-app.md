The Caretaker is a routine (`system/routines/caretaker.toml`, off by default, turned on from the Routines app or with `routines enable caretaker`) rather than a cron entry two skills wrote and removed. Its check wakes the agent through `routines wake caretaker`, so the run records the chat, and it adds a fifth finding, routines that are failing, overdue, broken or waiting on the user; `--only routines` runs that finding alone, the shape a lighter daily check can take.

A routines listing the check cannot read is logged in the run, rather than passing as nothing to report.
