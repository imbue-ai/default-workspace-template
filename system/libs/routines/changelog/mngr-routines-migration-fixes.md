`routines migrate-cron` converts a cron line with shell syntax (quotes, `&&`, pipes, `$` expansions) into a routine that runs the line's text through the entry's shell, as cron did, instead of splitting it into words that fail on every run. `\%` becomes a literal `%`, and a line whose bare `%` feeds the command input stays with cron.

It also carries over the state of jobs that never ran on minds-v0.8.7, which the old runner kept under `system/data/.state/jobs/`, so a migrated job keeps its last success and does not run again at update time.
