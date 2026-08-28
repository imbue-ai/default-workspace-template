# Globally unique terminal server_registered event ids

- The terminal app's `server_registered` discovery event id was derived from only the service name and URL, so every workspace (and every restart) emitted the same id -- and analytics, which collects these events fleet-wide and dedupes by event id, saw the whole fleet collapse to one event. The id now hashes in the emission timestamp, which is upgraded from hardcoded zero nanoseconds to real nanosecond precision.
