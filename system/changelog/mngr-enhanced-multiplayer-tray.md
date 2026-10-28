`system/scripts/forward_port.py` copies a launch path's `mention_param` onto the registry row beside its `text_param` and `draft_param` (the multiplayer spec, section 7.5).

The desktop interface's contract table (`docs/system/blueprint/desktop-interface/contracts.md`, section 2) lists the chat app's `lounge` launch path beside its other intake paths, and `system/test_app_manifests.py` holds the manifest to it.

The post-launch-paths plan (`docs/system/blueprint/post-launch-paths/plan-post-launch-paths.md`) lists the `lounge` intake target and launch path beside the others, and its launcher section (4) names the rows the chords run now that `main` is first: `main` is Enter, `new` is Ctrl+Enter, and a Getting Started tile's text goes to the main chat; the launcher plan (`docs/system/blueprint/launcher-and-getting-started/plan-launcher-and-getting-started.md`, sections 3.1, 3.5, 4.4, and 4.5) names the same rows in its stock-machine examples.
