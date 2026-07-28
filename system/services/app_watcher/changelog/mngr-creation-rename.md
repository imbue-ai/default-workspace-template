The workspace vocabulary and tree were renamed around "creations": users make apps (opened as tabs), skills (an automation is a skill run on a schedule), data, and customizations.

Moved from system/libs/ to system/services/ (a tab-less background service). Now watches data/.state/apps.toml ([[apps]] entries) instead of applications.toml.

Its README now accurately describes the events it writes (service_registered / service_deregistered into events/services/events.jsonl, with the 5-second polling fallback) -- it previously described the terminal's separate events/servers stream.
