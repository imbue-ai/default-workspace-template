The workspace vocabulary and tree were renamed around "creations": users make apps (opened as tabs), skills (an automation is a skill run on a schedule), data, and customizations.

Skills renamed to the new vocabulary: build-web-service -> build-app, update-service -> update-app, and the lifecycle leads crystallize/update/heal-artifact -> crystallize/update/heal-creation, parameterized by type: skill | app | service | system-interface. Worker references follow: harden-artifact.md -> harden-creation.md, artifact-*.md -> type-*.md, plus a new type-service.md for standalone daemons.

"creation snapshot" is now "template base" in the publish-inspiration and update-self flows. Scaffolded apps land in system/apps/<package>/ with DATA_DIR defaulting to data/.apps/<name>/. The show-files-in-chat guidance places shared files in visible homes (project folders, data/documents/, data/images/) instead of the removed chat-files/chat-images directories.
