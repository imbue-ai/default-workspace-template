Merge main into the self-hosted sharing branch: the share-gateway supervisord program uses the relocated oom_tag_service.py path from the services restructure, and the browser keeps main's headful-under-Xvfb shape while serving its own origin.

Remove the last vestiges of the deleted placeholder `web` example server: its entry in oom_priority's SERVICE_BANDS (a band for a program that no longer exists).
