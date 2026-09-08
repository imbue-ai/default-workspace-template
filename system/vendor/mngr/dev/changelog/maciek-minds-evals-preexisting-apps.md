The minds-eval-harbor outcome-verification spec describes how the evidence collector decides which
apps a workspace already served before the agent ran: one probe taken before turn 1 answers both the
app registry as it stood and the workspace's own `system/supervisord.conf` registrations, and their
union is the pre-existing set.

It explains why neither source is sufficient alone -- the terminal registers its port from inside
the script its supervisord program runs, so a config-only derivation would score it as the
deliverable, while a config that is on disk from the moment the workspace is cloned covers a service
too slow to have registered its port yet -- why the directory names under `system/apps/` are not the
registry's vocabulary, and why an unreadable registry leaves the set unknown rather than empty,
recorded as `error` entries with reason `preexisting_unknown`.
