The cron migration leaves the Imbue Studio app's `update-self-dispatch` cron entry in place: the app owns and rewrites that entry, so it is never converted into a routine.
