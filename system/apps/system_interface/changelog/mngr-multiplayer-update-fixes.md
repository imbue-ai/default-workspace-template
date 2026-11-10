- A visitor's desktop made before minds-v0.10.0 is theirs again. Older shells seeded each visitor's desktop with no `owner`, which 0.10.0 reads as the workspace owner's, so after the update a visitor loaded straight into the viewing chrome on their own desktop with nowhere for "Stop viewing" to take them. The shell now gives each ownerless desktop that `users.json` assigns to a visitor that visitor's key on its first start after the update.

- "Stop viewing" no longer does nothing when it finds no other desktop to go to: a toast says the window couldn't leave, because the workspace did not answer or there is no other desktop to go to.

- A visitor whose recorded desktop id now names somebody else's desktop (the owner deleted theirs and later made one that took the id) gets a fresh desktop of their own on arrival, with the replaced-desktop notice, instead of landing on the owner's desktop with nowhere to stop viewing to.

- The roster and the presence listing never wait on the connector. Profiles were re-fetched one person at a time inside the request every five minutes, so `GET /api/roster` and the heartbeat could take longer than the chat app's two-second limit. They now answer from the profile cache, a stale entry included, fetch what is missing or stale in the background, and announce the roster and the connected set again when a fetched profile changed.

- A granted person nothing names (no profile name, no email, no arrival yet) is called "Guest" in the roster instead of their account's raw id.
