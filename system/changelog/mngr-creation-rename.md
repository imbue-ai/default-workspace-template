The workspace vocabulary and tree were renamed around "creations": users make apps (opened as tabs), skills (an automation is a skill run on a schedule), data, and customizations.

The tree was restructured: system/libs/ split three ways into system/apps/ (everything tab-openable: system_interface, browser, the terminal wiring, and user-built apps), system/services/ (tab-less background daemons), and system/libs/ (support libraries). The creations/ folder is gone; top-level apps and skills symlinks point at system/apps/ and .agents/skills/ for discoverability.

The data/ layout follows the "everything visible is yours" convention: per-app data moved to data/.apps/<name>/, skills get data/.skills/<name>/, visible starter folders data/documents/ and data/my-project/ replace the removed data/chat-files/ and data/chat-images/ (shared files now live in sensible visible homes and are served in place).

The app registry moved from data/.state/applications.toml to data/.state/apps.toml ([[apps]] entries, MINDS_APPS_FILE override). Repo-wide ratchets now keep the retired terms (creations/ paths, "artifact", "web service", "application") out of live prose.
