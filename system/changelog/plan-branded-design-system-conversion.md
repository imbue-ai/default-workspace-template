The standard theme is now the Imbue brand, and the shipped themes are on theme contract 2.

- Classic Mac and Windows 2000 set the contract-2 roles and turn off computed hover shading, so they look as they did.
- On boot, `system/scripts/env.d/1300-migrate-workspace-themes.sh` moves any theme made in the workspace onto contract 2.
- The shared UI library and the page kit carry the brand's color table: keys, ramps, roles, and computed hover and active shades.
- `docs/system/themeable-apps.md` names the contract-2 color roles.
