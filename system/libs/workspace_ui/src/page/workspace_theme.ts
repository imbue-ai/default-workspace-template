/**
 * The page kit's script (docs/system/blueprint/workspace-themes/, section 5.3), built as one classic script every app
 * serves at `/_static/workspace_theme.js`: an app page built as plain HTML loads it in `<head>` after its own
 * stylesheet, and from then on wears the workspace's theme like a page built on the shared library does. It wears
 * the theme this origin last wore before the first paint, then whatever the shell names in `shell:theme`.
 */

import "./workspace_theme.css";
import { followShellTheme } from "../app_contract";
import { STANDARD_THEME_ID } from "../themes/themeBoot";
import { rememberedTheme, wearTheme, wearThemeFromMessage } from "../themes/themeClient";

const remembered = rememberedTheme();
// Put on while <head> is still being read, so holding the first frame until it loads costs no flash.
if (remembered.id !== STANDARD_THEME_ID) void wearTheme(remembered, document, { isRenderBlocking: true });

followShellTheme((theme, revision, isPreview) => void wearThemeFromMessage({ theme, revision, isPreview }));
