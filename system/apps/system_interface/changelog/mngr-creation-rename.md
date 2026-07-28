The workspace vocabulary and tree were renamed around "creations": users make apps (opened as tabs), skills (an automation is a skill run on a schedule), data, and customizations.

Moved from system/libs/ to system/apps/ (it is the special app that hosts the other tabs). The app registry it watches is now data/.state/apps.toml, with the WebSocket message renamed applications_updated -> apps_updated (field: apps) and AppEntry replacing ApplicationEntry across the backend and frontend.
