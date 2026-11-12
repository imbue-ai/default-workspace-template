The standard theme is now the Imbue brand, and the shipped themes are on theme contract 2.

- Classic Mac and Windows 2000 set the contract-2 roles and turn off computed hover shading, so they look as they did.
- On boot, `system/scripts/env.d/1300-migrate-workspace-themes.sh` moves any theme made in the workspace onto contract 2.
- The shared UI library and the page kit carry the brand's color table (keys, ramps, roles, and computed hover and active shades) and its type: Atkinson Hyperlegible Next for text, Atkinson Hyperlegible Mono for static labels and code, and Besley for headings, bundled under the SIL Open Font License, on the brand's ramp (`type-heading-lg` through `type-badge`, with `type-title-sm` for the title of a card, a request, or a row, and 13px `type-body-dense` for menus, segmented controls, and list rows).
- `docs/system/themeable-apps.md` names the contract-2 color roles.
