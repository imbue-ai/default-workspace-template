Keyboard shortcuts become rebindable, per account or per workspace (user-profile-state spec, phase 3C).

- New keybindings routes: the shell's own manifest and resolved chords (`/api/keybindings/actions`, `/me`), the whole-workspace catalog the Keyboard settings lists (`/catalog`, aggregating every app's manifest off the registry), and the save / reset / discard / deliver flows (`/api/keybindings`, `/reset`, `/discard`, `/deliver`).

- The owner's "everywhere" save lands in `data/system/profile/unsent.json` plus the owner's override file, so the workspace behaves as asked at once, and is delivered to the account through the latchkey gateway's `minds-workspaces-account-settings` verb; the first delivery 403s and files one `workspace` permission request, retried when the chrome announces the verdict (`minds:permission-resolutions`) or from the Keyboard settings, until it lands or is discarded there. A visitor's (or a "this workspace only") save is the requester's own override file.

- The backdrop's context menu gains "Keyboard shortcuts...", a dialog listing every app's actions from their manifests, with a chord recorder, per-app conflict checks, and the save-scope choice (with why "everywhere" is unavailable without an Imbue Studio connection).

- A `keybindings_changed` WebSocket broadcast and a `shell:keybindings-changed` push to every framed page make apps re-fetch their resolved chords; a watch on the profile snapshot announces changes other computers deliver. The launcher's secondary-send chord is the shell's first rebindable action.

- `workspace_profile` joins the update-staleness dependency lists.

- The viewing e2e test (`test_a_persons_card_lists_their_desktops_and_a_click_views_one_with_the_chrome_on`) is marked flaky: it fails intermittently in CI on the viewed window's visibility, on main as well, and passes locally.
