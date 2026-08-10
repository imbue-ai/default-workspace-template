# data/.skills/

Stored data a skill keeps for itself, one folder per skill name -- caches,
cursors, fetched payloads, anything a skill's scripts persist across runs.

Dot-prefixed because it is machinery-managed: the layout inside each folder
belongs to the skill's scripts, not to the user. Files a skill produces *for*
the user (documents, images) go in the visible `data/` folders instead.
